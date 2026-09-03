#!/usr/bin/env python3
"""
PLDT H153-381 Modem Analytics System - Setup Wizard
----------------------------------------------------
Interactive configuration helper that prompts for modem credentials
and network settings, tests connectivity, and saves them to .env.
"""

import os
import sys
import socket
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
ENV_EXAMPLE = os.path.join(BASE_DIR, ".env.example")

PLACEHOLDER_PASSWORDS = {
    "",
    "YOUR_MODEM_PASSWORD",
    "YOUR_MODEM_PASSWORD_HERE",
    "YOUR_ACTUAL_MODEM_PASSWORD",
}


def load_existing_env():
    config = {
        "MODEM_URL": "http://192.168.1.1/",
        "MODEM_USER": "pldthome",
        "MODEM_PASS": "",
        "PORT": "8085",
    }
    target = ENV_FILE if os.path.isfile(ENV_FILE) else ENV_EXAMPLE
    if os.path.isfile(target):
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip().strip('"').strip("'")
    return config


def test_modem_reachability(url: str, timeout: float = 3.0) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "192.168.1.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def run_wizard():
    print()
    print("=" * 70)
    print("   PLDT H153-381 Modem Analytics System - Interactive Setup")
    print("=" * 70)
    print(" This wizard will configure your modem connection and web dashboard.\n")

    current = load_existing_env()
    current_pass = current.get("MODEM_PASS", "")

    # If valid config already exists, check if user really wants to overwrite
    if os.path.isfile(ENV_FILE) and current_pass not in PLACEHOLDER_PASSWORDS:
        print(f" Current Configuration Detected:")
        print(f"  - Modem URL:  {current.get('MODEM_URL')}")
        print(f"  - Username:   {current.get('MODEM_USER')}")
        print(f"  - Port:       {current.get('PORT')}")
        print(f"  - Password:   {'*' * len(current_pass)}")
        print()
        try:
            choice = input(" Would you like to reconfigure these settings? [y/N]: ").strip().lower()
            if choice not in ("y", "yes"):
                print(" Keeping existing configuration.")
                return
        except (KeyboardInterrupt, EOFError):
            print("\n Setup aborted.")
            sys.exit(0)

    # 1. Modem Gateway URL
    default_url = current.get("MODEM_URL", "http://192.168.1.1/")
    print(f"\n [1/4] Modem Gateway IP / URL")
    print("       Default PLDT H153-381 gateway is http://192.168.1.1/")
    val = input(f"       Enter URL or IP [Default: {default_url}]: ").strip()
    if not val:
        modem_url = default_url
    else:
        if not val.startswith("http://") and not val.startswith("https://"):
            val = f"http://{val}"
        if not val.endswith("/"):
            val = f"{val}/"
        modem_url = val

    # 2. Modem Username
    default_user = current.get("MODEM_USER", "pldthome")
    print(f"\n [2/4] Modem Admin Username")
    print("       Usually 'pldthome' or 'admin' for PLDT Huawei CPEs.")
    val = input(f"       Enter Username [Default: {default_user}]: ").strip()
    modem_user = val if val else default_user

    # 3. Modem Admin Password
    print(f"\n [3/4] Modem Admin Password")
    print("       (Find this on the sticker under your modem, or your custom admin password)")
    while True:
        try:
            modem_pass = input("       Enter Password: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n Setup cancelled.")
            sys.exit(0)

        if not modem_pass or modem_pass in PLACEHOLDER_PASSWORDS:
            print("       [!] Password cannot be empty. Please enter your modem password.")
        else:
            break

    # 4. Dashboard Port
    default_port = current.get("PORT", "8085")
    print(f"\n [4/4] Dashboard Web Server Port")
    print(f"       Port where the web analytics dashboard will be served.")
    val = input(f"       Enter Port [Default: {default_port}]: ").strip()
    if val.isdigit():
        port = val
    else:
        port = default_port

    # Test Modem Reachability
    print("\n Testing connection to modem...")
    is_reachable = test_modem_reachability(modem_url)
    if is_reachable:
        print(f" [✓] Success: Modem is reachable at {modem_url}")
    else:
        print(f" [!] Notice: Could not reach {modem_url} right now.")
        print("     Please ensure your PC is connected to the PLDT WiFi or connected via LAN cable.")
        print("     (Saving your settings anyway so the poller connects when network is ready)")

    # Save to .env
    env_content = f"""# PLDT H153-381 Modem WAN Traffic & Throttling Analytics System Configuration

# Modem Gateway Web UI URL & Credentials
MODEM_URL={modem_url}

# Default username is 'pldthome' or 'admin'
MODEM_USER={modem_user}
MODEM_PASS={modem_pass}

# Analytics System Server Port
PORT={port}
"""
    try:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(env_content)
        print(f"\n [✓] Settings saved successfully to .env!")
        print("=" * 70)
        print()
    except Exception as e:
        print(f"\n [!] Error writing to .env: {e}")


if __name__ == "__main__":
    run_wizard()
