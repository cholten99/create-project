#!/usr/bin/env python3

import argparse
import base64
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
import yaml
from dotenv import load_dotenv
from nacl import encoding, public


TOTAL_STEPS = 11
DNS_WAIT_TIMEOUT_SECONDS = 180
DNS_WAIT_INTERVAL_SECONDS = 10


@dataclass
class AppConfig:
    local_dev_root: Path
    remote_web_root: str
    apache_sites_path: str
    certbot_email: str
    cloudflare_proxy: bool
    github_user: str


@dataclass
class EnvConfig:
    github_token: str
    cloudflare_api_token: str
    server_ip: str
    server_ssh_user: str
    server_ssh_key_path: Path
    sftp_host: str
    sftp_username: str
    sftp_private_key_path: Path


@dataclass
class RuntimeConfig:
    project_name: str
    project_domain: str
    dry_run: bool
    verbose: bool
    start_step: int
    end_step: int
    repo_root: Path
    templates_project_dir: Path
    templates_apache_file: Path


class StepError(Exception):
    """Raised when a provisioning step fails."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and provision a new static website project."
    )
    parser.add_argument("project_name", help="Short project name, e.g. ukgovcomms")
    parser.add_argument("project_domain", help="Project domain, e.g. ukgovcomms.org")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making changes",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--start-step",
        type=int,
        default=1,
        help=f"First step to run (default: 1, max: {TOTAL_STEPS})",
    )
    parser.add_argument(
        "--end-step",
        type=int,
        default=TOTAL_STEPS,
        help=f"Last step to run (default: {TOTAL_STEPS})",
    )
    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_yaml_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise StepError(f"Config file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw: dict[str, Any] = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise StepError(f"Could not parse YAML config {config_path}: {exc}") from exc
    except OSError as exc:
        raise StepError(f"Could not read config file {config_path}: {exc}") from exc

    required_keys = [
        "local_dev_root",
        "remote_web_root",
        "apache_sites_path",
        "certbot_email",
        "cloudflare_proxy",
        "github_user",
    ]
    missing = [key for key in required_keys if key not in raw]
    if missing:
        raise StepError(
            f"Missing required config.yaml keys: {', '.join(sorted(missing))}"
        )

    return AppConfig(
        local_dev_root=Path(str(raw["local_dev_root"])).expanduser(),
        remote_web_root=str(raw["remote_web_root"]),
        apache_sites_path=str(raw["apache_sites_path"]),
        certbot_email=str(raw["certbot_email"]),
        cloudflare_proxy=bool(raw["cloudflare_proxy"]),
        github_user=str(raw["github_user"]).strip(),
    )


def load_env_config(env_path: Path) -> EnvConfig:
    if not env_path.exists():
        raise StepError(f".env file not found: {env_path}")

    load_dotenv(dotenv_path=env_path)

    required_vars = [
        "GITHUB_TOKEN",
        "CLOUDFLARE_API_TOKEN",
        "SERVER_IP",
        "SERVER_SSH_USER",
        "SERVER_SSH_KEY_PATH",
        "SFTP_HOST",
        "SFTP_USERNAME",
        "SFTP_PRIVATE_KEY_PATH",
    ]

    missing = [name for name in required_vars if not os.getenv(name)]
    if missing:
        raise StepError(
            f"Missing required .env values: {', '.join(sorted(missing))}"
        )

    return EnvConfig(
        github_token=os.environ["GITHUB_TOKEN"],
        cloudflare_api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        server_ip=os.environ["SERVER_IP"],
        server_ssh_user=os.environ["SERVER_SSH_USER"],
        server_ssh_key_path=Path(os.environ["SERVER_SSH_KEY_PATH"]).expanduser(),
        sftp_host=os.environ["SFTP_HOST"],
        sftp_username=os.environ["SFTP_USERNAME"],
        sftp_private_key_path=Path(os.environ["SFTP_PRIVATE_KEY_PATH"]).expanduser(),
    )


def build_runtime_config(args: argparse.Namespace, repo_root: Path) -> RuntimeConfig:
    return RuntimeConfig(
        project_name=args.project_name,
        project_domain=args.project_domain,
        dry_run=args.dry_run,
        verbose=args.verbose,
        start_step=args.start_step,
        end_step=args.end_step,
        repo_root=repo_root,
        templates_project_dir=repo_root / "templates" / "project",
        templates_apache_file=repo_root / "templates" / "apache" / "project-template.conf",
    )


def validate_paths(
    app_config: AppConfig, env_config: EnvConfig, runtime: RuntimeConfig
) -> None:
    path_checks: list[tuple[str, Path, bool]] = [
        ("local_dev_root", app_config.local_dev_root, True),
        ("templates project directory", runtime.templates_project_dir, True),
        ("apache template file", runtime.templates_apache_file, False),
        ("server SSH key", env_config.server_ssh_key_path, False),
        ("SFTP private key", env_config.sftp_private_key_path, False),
    ]

    for label, path, is_dir in path_checks:
        if not path.exists():
            raise StepError(f"Required {label} does not exist: {path}")
        if is_dir and not path.is_dir():
            raise StepError(f"Expected {label} to be a directory: {path}")
        if not is_dir and not path.is_file():
            raise StepError(f"Expected {label} to be a file: {path}")


def validate_inputs(runtime: RuntimeConfig) -> None:
    if not runtime.project_name.strip():
        raise StepError("Project name must not be empty")

    if not runtime.project_domain.strip():
        raise StepError("Project domain must not be empty")

    if " " in runtime.project_name:
        raise StepError("Project name must not contain spaces")

    if " " in runtime.project_domain:
        raise StepError("Project domain must not contain spaces")

    if "." not in runtime.project_domain:
        raise StepError("Project domain does not look valid")

    if runtime.start_step < 1 or runtime.start_step > TOTAL_STEPS:
        raise StepError(f"--start-step must be between 1 and {TOTAL_STEPS}")

    if runtime.end_step < 1 or runtime.end_step > TOTAL_STEPS:
        raise StepError(f"--end-step must be between 1 and {TOTAL_STEPS}")

    if runtime.start_step > runtime.end_step:
        raise StepError("--start-step must be less than or equal to --end-step")


def log_step(step_number: int, message: str) -> None:
    logging.info("Step %s: %s", step_number, message)


def maybe_run(description: str, dry_run: bool) -> None:
    if dry_run:
        logging.info("[DRY RUN] %s", description)
    else:
        logging.info("%s", description)


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    command_str = " ".join(shlex.quote(part) for part in command)

    if dry_run:
        logging.info("[DRY RUN] Would run command: %s", command_str)
        return None

    logging.debug("Running command: %s", command_str)

    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout.strip() if exc.stdout else ""
        stderr = exc.stderr.strip() if exc.stderr else ""
        message_parts = [f"Command failed: {command_str}"]
        if stdout:
            message_parts.append(f"stdout: {stdout}")
        if stderr:
            message_parts.append(f"stderr: {stderr}")
        raise StepError(" | ".join(message_parts)) from exc
    except OSError as exc:
        raise StepError(f"Could not run command {command_str}: {exc}") from exc

    if result.stdout.strip():
        logging.debug("Command stdout: %s", result.stdout.strip())
    if result.stderr.strip():
        logging.debug("Command stderr: %s", result.stderr.strip())

    return result


def github_headers(env_config: EnvConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env_config.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def cloudflare_headers(env_config: EnvConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env_config.cloudflare_api_token}",
        "Content-Type": "application/json",
    }


def github_request(
    method: str,
    url: str,
    env_config: EnvConfig,
    *,
    json_payload: dict[str, Any] | None = None,
    expected_statuses: tuple[int, ...] = (200,),
) -> requests.Response:
    try:
        response = requests.request(
            method,
            url,
            headers=github_headers(env_config),
            json=json_payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise StepError(f"GitHub API request failed for {url}: {exc}") from exc

    if response.status_code not in expected_statuses:
        try:
            details = response.json()
        except ValueError:
            details = response.text.strip()

        raise StepError(
            f"GitHub API request failed: {method} {url} returned "
            f"{response.status_code}: {details}"
        )

    return response


def cloudflare_request(
    method: str,
    endpoint: str,
    env_config: EnvConfig,
    *,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    expected_statuses: tuple[int, ...] = (200,),
) -> dict[str, Any]:
    url = f"https://api.cloudflare.com/client/v4{endpoint}"

    try:
        response = requests.request(
            method,
            url,
            headers=cloudflare_headers(env_config),
            params=params,
            json=json_payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise StepError(f"Cloudflare API request failed for {url}: {exc}") from exc

    if response.status_code not in expected_statuses:
        try:
            details = response.json()
        except ValueError:
            details = response.text.strip()

        raise StepError(
            f"Cloudflare API request failed: {method} {url} returned "
            f"{response.status_code}: {details}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise StepError(f"Cloudflare API returned non-JSON response for {url}") from exc

    if not data.get("success", False):
        raise StepError(
            f"Cloudflare API reported failure for {method} {url}: "
            f"{data.get('errors', data)}"
        )

    return data


def github_repo_exists(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> bool:
    url = (
        f"https://api.github.com/repos/"
        f"{app_config.github_user}/{runtime.project_name}"
    )

    try:
        response = requests.get(
            url,
            headers=github_headers(env_config),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise StepError(f"GitHub API request failed for {url}: {exc}") from exc

    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False

    try:
        details = response.json()
    except ValueError:
        details = response.text.strip()

    raise StepError(
        f"GitHub API request failed: GET {url} returned "
        f"{response.status_code}: {details}"
    )


def create_github_repo(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> None:
    url = "https://api.github.com/user/repos"
    payload = {
        "name": runtime.project_name,
        "private": True,
        "auto_init": False,
    }

    github_request(
        "POST",
        url,
        env_config,
        json_payload=payload,
        expected_statuses=(201,),
    )

    logging.info(
        "Created GitHub repository %s/%s",
        app_config.github_user,
        runtime.project_name,
    )


def get_github_actions_public_key(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> tuple[str, str]:
    url = (
        f"https://api.github.com/repos/"
        f"{app_config.github_user}/{runtime.project_name}/actions/secrets/public-key"
    )

    response = github_request(
        "GET",
        url,
        env_config,
        expected_statuses=(200,),
    )

    data = response.json()

    key = data.get("key")
    key_id = data.get("key_id")

    if not key or not key_id:
        raise StepError("GitHub did not return a valid Actions public key payload")

    return key, key_id


def encrypt_github_secret(public_key_value: str, secret_value: str) -> str:
    try:
        public_key = public.PublicKey(
            public_key_value.encode("utf-8"),
            encoding.Base64Encoder(),
        )
        sealed_box = public.SealedBox(public_key)
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        return base64.b64encode(encrypted).decode("utf-8")
    except Exception as exc:
        raise StepError(f"Failed to encrypt GitHub secret: {exc}") from exc


def set_github_actions_secret(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
    *,
    secret_name: str,
    secret_value: str,
    public_key_value: str,
    key_id: str,
) -> None:
    encrypted_value = encrypt_github_secret(public_key_value, secret_value)

    url = (
        f"https://api.github.com/repos/"
        f"{app_config.github_user}/{runtime.project_name}/actions/secrets/{secret_name}"
    )

    payload = {
        "encrypted_value": encrypted_value,
        "key_id": key_id,
    }

    github_request(
        "PUT",
        url,
        env_config,
        json_payload=payload,
        expected_statuses=(201, 204),
    )

    logging.info("Set GitHub Actions secret %s", secret_name)


def read_text_file(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StepError(f"Could not read {label} at {path}: {exc}") from exc


def render_template_text(text: str, runtime: RuntimeConfig) -> str:
    replacements = {
        "<<project-name>>": runtime.project_name,
        "<<project-domain>>": runtime.project_domain,
    }

    rendered = text
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)

    return rendered


def render_apache_config(runtime: RuntimeConfig) -> str:
    try:
        template_text = runtime.templates_apache_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise StepError(
            f"Could not read Apache template file {runtime.templates_apache_file}: {exc}"
        ) from exc

    return render_template_text(template_text, runtime)


def ssh_base_command(env_config: EnvConfig) -> list[str]:
    return [
        "ssh",
        "-i",
        str(env_config.server_ssh_key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{env_config.server_ssh_user}@{env_config.server_ip}",
    ]


def scp_base_command(env_config: EnvConfig) -> list[str]:
    return [
        "scp",
        "-i",
        str(env_config.server_ssh_key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]


def run_ssh_command(
    env_config: EnvConfig,
    remote_command: str,
    *,
    dry_run: bool = False,
) -> None:
    run_command(
        ssh_base_command(env_config) + [remote_command],
        dry_run=dry_run,
    )


def copy_file_to_server(
    env_config: EnvConfig,
    local_path: Path,
    remote_path: str,
    *,
    dry_run: bool = False,
) -> None:
    destination = f"{env_config.server_ssh_user}@{env_config.server_ip}:{remote_path}"
    run_command(
        scp_base_command(env_config) + [str(local_path), destination],
        dry_run=dry_run,
    )


def copy_directory_contents(source_dir: Path, target_dir: Path) -> None:
    if not source_dir.is_dir():
        raise StepError(f"Template directory does not exist: {source_dir}")
    if not target_dir.is_dir():
        raise StepError(f"Target directory does not exist: {target_dir}")

    for entry in source_dir.iterdir():
        destination = target_dir / entry.name

        if destination.exists():
            raise StepError(
                f"Refusing to overwrite existing path in target repo: {destination}"
            )

        try:
            if entry.is_dir():
                shutil.copytree(entry, destination)
            else:
                shutil.copy2(entry, destination)
        except Exception as exc:
            raise StepError(
                f"Failed to copy template item {entry} to {destination}: {exc}"
            ) from exc


def list_cloudflare_zones(env_config: EnvConfig) -> list[dict[str, Any]]:
    page = 1
    per_page = 50
    zones: list[dict[str, Any]] = []

    while True:
        data = cloudflare_request(
            "GET",
            "/zones",
            env_config,
            params={"page": page, "per_page": per_page},
            expected_statuses=(200,),
        )

        zones.extend(data.get("result", []))
        result_info = data.get("result_info", {})
        total_pages = result_info.get("total_pages", 1)

        if page >= total_pages:
            break

        page += 1

    return zones


def find_best_matching_cloudflare_zone(
    domain: str,
    env_config: EnvConfig,
) -> dict[str, Any]:
    zones = list_cloudflare_zones(env_config)
    domain_lower = domain.lower().rstrip(".")

    matches = []
    for zone in zones:
        zone_name = str(zone.get("name", "")).lower().rstrip(".")
        if domain_lower == zone_name or domain_lower.endswith(f".{zone_name}"):
            matches.append(zone)

    if not matches:
        raise StepError(
            f"No Cloudflare zone found matching domain {domain}. "
            "Make sure the parent zone exists in your Cloudflare account."
        )

    matches.sort(key=lambda z: len(str(z.get("name", ""))), reverse=True)
    return matches[0]


def compute_record_name_for_zone(domain: str, zone_name: str) -> str:
    domain_norm = domain.lower().rstrip(".")
    zone_norm = zone_name.lower().rstrip(".")

    if domain_norm == zone_norm:
        return "@"

    suffix = f".{zone_norm}"
    if not domain_norm.endswith(suffix):
        raise StepError(
            f"Domain {domain} does not belong to Cloudflare zone {zone_name}"
        )

    relative = domain_norm[: -len(suffix)]
    if not relative:
        return "@"

    return relative


def list_cloudflare_dns_records(
    zone_id: str,
    full_domain_name: str,
    env_config: EnvConfig,
) -> list[dict[str, Any]]:
    data = cloudflare_request(
        "GET",
        f"/zones/{zone_id}/dns_records",
        env_config,
        params={"name": full_domain_name},
        expected_statuses=(200,),
    )
    return data.get("result", [])


def upsert_cloudflare_a_record(
    domain: str,
    target_ip: str,
    proxied: bool,
    env_config: EnvConfig,
) -> None:
    zone = find_best_matching_cloudflare_zone(domain, env_config)
    zone_id = str(zone["id"])
    zone_name = str(zone["name"])

    record_name = compute_record_name_for_zone(domain, zone_name)
    existing_records = list_cloudflare_dns_records(zone_id, domain, env_config)

    conflicting_records = [
        record for record in existing_records if record.get("type") != "A"
    ]
    if conflicting_records:
        conflict_types = ", ".join(
            sorted({str(record.get("type", "unknown")) for record in conflicting_records})
        )
        raise StepError(
            f"Refusing to create A record for {domain} because conflicting "
            f"record type(s) already exist: {conflict_types}"
        )

    existing_a_records = [
        record for record in existing_records if record.get("type") == "A"
    ]

    payload = {
        "type": "A",
        "name": record_name,
        "content": target_ip,
        "proxied": proxied,
        "ttl": 1,
    }

    if existing_a_records:
        record_id = str(existing_a_records[0]["id"])
        cloudflare_request(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{record_id}",
            env_config,
            json_payload=payload,
            expected_statuses=(200,),
        )
        logging.info(
            "Updated Cloudflare A record for %s in zone %s -> %s",
            domain,
            zone_name,
            target_ip,
        )
    else:
        cloudflare_request(
            "POST",
            f"/zones/{zone_id}/dns_records",
            env_config,
            json_payload=payload,
            expected_statuses=(200,),
        )
        logging.info(
            "Created Cloudflare A record for %s in zone %s -> %s",
            domain,
            zone_name,
            target_ip,
        )


def dig_resolved_ips(domain: str) -> list[str]:
    result = run_command(
        ["dig", "+short", domain],
        dry_run=False,
    )
    assert result is not None

    ips = []
    for line in result.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        if value.endswith("."):
            # CNAME or other hostname target, not an IP address
            continue
        ips.append(value)

    return ips


def wait_for_dns_resolution(domain: str, expected_ip: str) -> None:
    deadline = time.time() + DNS_WAIT_TIMEOUT_SECONDS

    while True:
        resolved_ips = dig_resolved_ips(domain)
        logging.info("DNS lookup for %s returned: %s", domain, resolved_ips or ["<none>"])

        if expected_ip in resolved_ips:
            logging.info("DNS propagation confirmed for %s -> %s", domain, expected_ip)
            return

        if time.time() >= deadline:
            raise StepError(
                f"Timed out waiting for DNS propagation for {domain}. "
                f"Expected {expected_ip}, got {resolved_ips or ['<none>']}"
            )

        time.sleep(DNS_WAIT_INTERVAL_SECONDS)


def step_1_validate_project_does_not_exist(
    app_config: AppConfig, runtime: RuntimeConfig
) -> None:
    log_step(1, "Validate local project folder does not already exist")
    target_dir = app_config.local_dev_root / runtime.project_name
    if target_dir.exists():
        raise StepError(f"Target project directory already exists: {target_dir}")
    maybe_run(f"Local project directory is available: {target_dir}", runtime.dry_run)


def step_2_create_dns_record(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> None:
    log_step(2, "Create Cloudflare DNS record")

    if runtime.dry_run:
        logging.info(
            "[DRY RUN] Would create or update Cloudflare A record for %s -> %s (proxied=%s)",
            runtime.project_domain,
            env_config.server_ip,
            app_config.cloudflare_proxy,
        )
        return

    upsert_cloudflare_a_record(
        runtime.project_domain,
        env_config.server_ip,
        app_config.cloudflare_proxy,
        env_config,
    )


def step_3_wait_for_dns(runtime: RuntimeConfig, env_config: EnvConfig) -> None:
    log_step(3, "Wait for DNS propagation")

    if runtime.dry_run:
        logging.info(
            "[DRY RUN] Would wait until %s resolves to %s",
            runtime.project_domain,
            env_config.server_ip,
        )
        return

    wait_for_dns_resolution(runtime.project_domain, env_config.server_ip)


def step_4_create_github_repo(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> None:
    log_step(4, "Create GitHub repository")

    repo_full_name = f"{app_config.github_user}/{runtime.project_name}"

    if runtime.dry_run:
        logging.info("[DRY RUN] Would check whether repo exists: %s", repo_full_name)
        logging.info("[DRY RUN] Would create GitHub repository: %s", repo_full_name)
        return

    if github_repo_exists(app_config, env_config, runtime):
        raise StepError(f"GitHub repository already exists: {repo_full_name}")

    create_github_repo(app_config, env_config, runtime)


def step_5_clone_github_repo(app_config: AppConfig, runtime: RuntimeConfig) -> None:
    log_step(5, "Clone GitHub repository into local development folder")

    target_dir = app_config.local_dev_root / runtime.project_name
    repo_url = (
        f"git@github.com:{app_config.github_user}/{runtime.project_name}.git"
    )

    run_command(
        ["git", "clone", repo_url, str(target_dir)],
        dry_run=runtime.dry_run,
    )


def step_6_copy_template_into_repo(app_config: AppConfig, runtime: RuntimeConfig) -> None:
    log_step(6, "Copy template contents into cloned repository")

    target_dir = app_config.local_dev_root / runtime.project_name

    if runtime.dry_run:
        logging.info(
            "[DRY RUN] Would copy contents of %s into %s",
            runtime.templates_project_dir,
            target_dir,
        )
        return

    copy_directory_contents(runtime.templates_project_dir, target_dir)
    logging.info("Template contents copied into %s", target_dir)


def step_7_update_local_templates(app_config: AppConfig, runtime: RuntimeConfig) -> None:
    log_step(7, "Replace template placeholders in local project files")

    target_dir = app_config.local_dev_root / runtime.project_name

    if runtime.dry_run:
        logging.info(
            "[DRY RUN] Would replace placeholders in files under %s",
            target_dir,
        )
        return

    updated_files = 0

    for path in target_dir.rglob("*"):
        if not path.is_file():
            continue

        if ".git" in path.parts:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue

        rendered = render_template_text(text, runtime)

        if rendered != text:
            try:
                path.write_text(rendered, encoding="utf-8")
                updated_files += 1
                logging.debug("Updated placeholders in %s", path)
            except Exception as exc:
                raise StepError(f"Failed updating {path}: {exc}") from exc

    logging.info("Updated placeholders in %s file(s)", updated_files)


def step_8_set_github_secrets(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> None:
    log_step(8, "Set GitHub Actions secrets")

    secret_names = [
        "SFTP_HOST",
        "SFTP_USERNAME",
        "SFTP_PRIVATE_KEY",
    ]

    if runtime.dry_run:
        logging.info(
            "[DRY RUN] Would fetch GitHub Actions public key for %s/%s",
            app_config.github_user,
            runtime.project_name,
        )
        logging.info(
            "[DRY RUN] Would set secrets: %s",
            ", ".join(secret_names),
        )
        return

    private_key_content = read_text_file(
        env_config.sftp_private_key_path,
        "SFTP private key",
    )

    public_key_value, key_id = get_github_actions_public_key(
        app_config,
        env_config,
        runtime,
    )

    secrets_to_set = {
        "SFTP_HOST": env_config.sftp_host,
        "SFTP_USERNAME": env_config.sftp_username,
        "SFTP_PRIVATE_KEY": private_key_content,
    }

    for secret_name, secret_value in secrets_to_set.items():
        set_github_actions_secret(
            app_config,
            env_config,
            runtime,
            secret_name=secret_name,
            secret_value=secret_value,
            public_key_value=public_key_value,
            key_id=key_id,
        )


def step_9_prepare_server(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> None:
    log_step(9, "Prepare remote web root and Apache configuration")

    remote_web_root = f"{app_config.remote_web_root}/{runtime.project_name}"
    remote_conf_path = f"{app_config.apache_sites_path}/{runtime.project_name}.conf"
    remote_tmp_conf_path = f"/tmp/{runtime.project_name}.conf"

    if runtime.dry_run:
        logging.info("[DRY RUN] Would create remote web root: %s", remote_web_root)
        logging.info(
            "[DRY RUN] Would render Apache config and copy it to %s",
            remote_tmp_conf_path,
        )
        logging.info(
            "[DRY RUN] Would move Apache config into place at %s",
            remote_conf_path,
        )
        logging.info(
            "[DRY RUN] Would chmod Apache config to 644, enable site %s and reload Apache",
            runtime.project_name,
        )
        return

    apache_config_text = render_apache_config(runtime)

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, suffix=".conf"
    ) as handle:
        handle.write(apache_config_text)
        local_temp_conf = Path(handle.name)

    try:
        run_ssh_command(
            env_config,
            f"sudo mkdir -p {shlex.quote(remote_web_root)}",
            dry_run=False,
        )

        copy_file_to_server(
            env_config,
            local_temp_conf,
            remote_tmp_conf_path,
            dry_run=False,
        )

        run_ssh_command(
            env_config,
            " && ".join(
                [
                    f"sudo mv {shlex.quote(remote_tmp_conf_path)} {shlex.quote(remote_conf_path)}",
                    f"sudo chmod 644 {shlex.quote(remote_conf_path)}",
                    f"sudo a2ensite {shlex.quote(runtime.project_name)}.conf",
                    "sudo systemctl reload apache2",
                ]
            ),
            dry_run=False,
        )
    finally:
        try:
            local_temp_conf.unlink(missing_ok=True)
        except OSError:
            pass

    logging.info("Remote web root prepared and Apache site enabled")


def step_10_run_certbot(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> None:
    log_step(10, "Create TLS certificate with certbot")

    certbot_command = (
        "sudo certbot --apache "
        f"-d {shlex.quote(runtime.project_domain)} "
        f"-m {shlex.quote(app_config.certbot_email)} "
        "--agree-tos --non-interactive"
    )

    if runtime.dry_run:
        logging.info("[DRY RUN] Would run remote certbot command: %s", certbot_command)
        return

    run_ssh_command(
        env_config,
        certbot_command,
        dry_run=False,
    )

    logging.info("Certbot completed for %s", runtime.project_domain)


def step_11_initial_commit_and_push(
    app_config: AppConfig,
    runtime: RuntimeConfig,
) -> None:
    log_step(11, "Create initial commit and push to GitHub")

    target_dir = app_config.local_dev_root / runtime.project_name

    if runtime.dry_run:
        logging.info(
            "[DRY RUN] Would run git add/commit/branch/push in %s",
            target_dir,
        )
        return

    if not (target_dir / ".git").exists():
        raise StepError(f"Not a git repository: {target_dir}")

    run_command(
        ["git", "add", "."],
        cwd=target_dir,
        dry_run=False,
    )

    try:
        run_command(
            ["git", "commit", "-m", "Initial commit"],
            cwd=target_dir,
            dry_run=False,
        )
    except StepError as exc:
        if "nothing to commit" in str(exc):
            logging.info("Nothing to commit (already clean)")
        else:
            raise

    run_command(
        ["git", "branch", "-M", "main"],
        cwd=target_dir,
        dry_run=False,
    )

    run_command(
        ["git", "push", "-u", "origin", "main"],
        cwd=target_dir,
        dry_run=False,
    )

    logging.info("Initial commit pushed to GitHub")


def build_steps(
    app_config: AppConfig,
    env_config: EnvConfig,
    runtime: RuntimeConfig,
) -> list[tuple[int, Callable[[], None]]]:
    return [
        (1, lambda: step_1_validate_project_does_not_exist(app_config, runtime)),
        (2, lambda: step_2_create_dns_record(app_config, env_config, runtime)),
        (3, lambda: step_3_wait_for_dns(runtime, env_config)),
        (4, lambda: step_4_create_github_repo(app_config, env_config, runtime)),
        (5, lambda: step_5_clone_github_repo(app_config, runtime)),
        (6, lambda: step_6_copy_template_into_repo(app_config, runtime)),
        (7, lambda: step_7_update_local_templates(app_config, runtime)),
        (8, lambda: step_8_set_github_secrets(app_config, env_config, runtime)),
        (9, lambda: step_9_prepare_server(app_config, env_config, runtime)),
        (10, lambda: step_10_run_certbot(app_config, env_config, runtime)),
        (11, lambda: step_11_initial_commit_and_push(app_config, runtime)),
    ]


def main() -> int:
    try:
        args = parse_args()
        configure_logging(args.verbose)

        repo_root = Path(__file__).resolve().parent
        logging.debug("Repo root: %s", repo_root)

        runtime = build_runtime_config(args, repo_root)
        validate_inputs(runtime)

        config_path = repo_root / "config.yaml"
        env_path = repo_root / ".env"

        app_config = load_yaml_config(config_path)
        env_config = load_env_config(env_path)
        validate_paths(app_config, env_config, runtime)

        logging.info("Starting create-project")
        logging.info("Project name: %s", runtime.project_name)
        logging.info("Project domain: %s", runtime.project_domain)
        logging.info("Dry run: %s", runtime.dry_run)
        logging.info(
            "Running steps: %s to %s",
            runtime.start_step,
            runtime.end_step,
        )

        for step_number, step_callable in build_steps(app_config, env_config, runtime):
            if runtime.start_step <= step_number <= runtime.end_step:
                step_callable()

        logging.info("Selected steps completed successfully")
        return 0

    except StepError as exc:
        logging.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logging.error("Interrupted by user")
        return 130
    except Exception as exc:
        logging.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
