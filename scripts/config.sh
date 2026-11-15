# shellcheck shell=bash disable=SC2034
#
# Copyright (C) 2026 by Fabrizio Giudici
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Settings and helpers shared by scripts/prepare-vm.sh and bin/BridgeBox (sourced, not executed).
# Settings come from environment variables (see the defaults below; the Makefile exports them).
# Besides vfkit (looked up in PATH, or set VFKIT to its path), only tools that ship with macOS are used.
# Note: this file must stay compatible with the bash 3.2 that ships with macOS.
#

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ALPINE_VERSION="${ALPINE_VERSION:-3.24.4}"
ALPINE_MIRROR="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
# The raw.tar.gz format (made for Google Cloud) is the same image as the qcow2 one, but in the raw format that vfkit
# needs: so no conversion tool is required.
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine-${ALPINE_VERSION}-aarch64-cloudinit-r0.raw.tar.gz}"
CPUS="${CPUS:-2}"
MEMORY_MB="${MEMORY_MB:-1024}"
MAC="${MAC:-02:bb:b0:00:00:01}"     # fixed, locally administered: used to find the VM in the DHCP leases
CACHE_DIR="${CACHE_DIR:-$ROOT/cache}"
VM_DIR="${VM_DIR:-$ROOT/target/vm}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-120}"
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"

SSH_USER=alpine                      # the default user of the Alpine cloud images
VFKIT="${VFKIT:-vfkit}"
DISK="$VM_DIR/disk.raw"
EFI_VARS="$VM_DIR/efi-vars"
SSH_KEY="$VM_DIR/id_ed25519"
KNOWN_HOSTS="$VM_DIR/known_hosts"
CLOUD_INIT_DIR="$VM_DIR/cloud-init"
BACKEND=/usr/local/sbin/bridgebox-backend   # inside the VM: installed from vm/bridgebox-backend.py
# The REST socket can't live in VM_DIR: macOS limits Unix socket paths to 104 bytes, which a deep checkout exceeds.
# /tmp is shared among users, so the directory is private and checked before use (see prepare_socket_dir).
SOCKET_DIR="/tmp/bridgebox-$(id -u)"
SOCKET="$SOCKET_DIR/vfkit.sock"
PID_FILE="$VM_DIR/vfkit.pid"
VFKIT_LOG="$VM_DIR/vfkit.log"
CONSOLE_LOG="$VM_DIR/console.log"
SEALED="$VM_DIR/sealed"             # written by scripts/prepare-vm.sh when the image has been sealed
PROVISIONED="$VM_DIR/provisioned"   # written by scripts/prepare-vm.sh when the VM has been fully prepared
LEASES="${LEASES:-/var/db/dhcpd_leases}"

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

need() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || die "'$tool' not found"
    done
}

# vfkit separates device options with commas.
check_paths() {
    case "$VM_DIR" in *,*) die "the path '$VM_DIR' contains a comma, which vfkit can't handle" ;; esac
}
