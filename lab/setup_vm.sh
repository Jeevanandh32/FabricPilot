#!/usr/bin/env bash
# One-time setup for the GCP lab VM (Ubuntu 24.04, nested virtualization enabled).
# Usage on the VM:   bash lab/setup_vm.sh
# Then copy your images into ~/images/ and run:   bash lab/setup_vm.sh images
set -euo pipefail

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

if [[ "${1:-}" != "images" ]]; then
  step "Checking nested virtualization"
  if [[ "$(grep -cwE 'vmx|svm' /proc/cpuinfo)" -eq 0 ]]; then
    echo "No vmx/svm CPU flag: recreate the VM with --enable-nested-virtualization" >&2
    exit 1
  fi

  step "Installing Docker, KVM tools, Python, git"
  sudo apt-get update -q
  sudo apt-get install -yq docker.io docker-compose-v2 qemu-kvm git make python3-venv python3-pip
  sudo usermod -aG docker "$USER"

  step "Installing containerlab"
  bash -c "$(curl -sL https://get.containerlab.dev)"

  step "Fetching vrnetlab (turns vendor qcow2 images into containers)"
  [[ -d ~/vrnetlab ]] || git clone --depth 1 https://github.com/srl-labs/vrnetlab ~/vrnetlab

  step "Starting NetBox (netbox-docker)"
  if [[ ! -d ~/netbox-docker ]]; then
    git clone -b release --depth 1 https://github.com/netbox-community/netbox-docker ~/netbox-docker
    cat > ~/netbox-docker/docker-compose.override.yml <<'YAML'
services:
  netbox:
    ports:
      - "127.0.0.1:8000:8080"
YAML
  fi
  # First start runs DB migrations and can outlast compose's health timeout; wait instead of failing.
  (cd ~/netbox-docker && sudo docker compose pull -q && { sudo docker compose up -d || true; })
  printf 'Waiting for NetBox (first start can take ~10 min)'
  for _ in $(seq 1 90); do
    curl -sf -o /dev/null http://127.0.0.1:8000/login/ && break
    printf '.'; sleep 10
  done
  curl -sf -o /dev/null http://127.0.0.1:8000/login/ && echo " up" || echo " still starting; check: cd ~/netbox-docker && sudo docker compose ps"
  (cd ~/netbox-docker && sudo docker compose up -d >/dev/null 2>&1 || true)

  step "Python environment for the pipeline"
  python3 -m venv ~/fabric-venv
  ~/fabric-venv/bin/pip install -q -r "$(dirname "$0")/../requirements.txt" pynetbox

  mkdir -p ~/images
  cat <<MSG

Base setup done. Next:
  1. Log out and back in (docker group).
  2. Copy images to the VM, e.g. from your Mac:
       gcloud compute scp nexus9300v64.10.4.3.F.qcow2 vJunos-switch-23.2R1.14.qcow2 evpn-lab:~/images/ --zone=us-east1-b
  3. bash lab/setup_vm.sh images
  4. NetBox admin user:
       cd ~/netbox-docker && sudo docker compose exec netbox /opt/netbox/netbox/manage.py createsuperuser
     Reach the UI from your Mac with:
       gcloud compute ssh evpn-lab --zone=us-east1-b -- -L 8000:127.0.0.1:8000
MSG
  exit 0
fi

step "Building vrnetlab images from ~/images"
shopt -s nullglob
n9k=(~/images/nexus9300v*.qcow2)
vjs=(~/images/vJunos-switch*.qcow2 ~/images/vjunos-switch*.qcow2)
[[ ${#n9k[@]} -gt 0 ]] || { echo "No nexus9300v*.qcow2 in ~/images" >&2; exit 1; }
[[ ${#vjs[@]} -gt 0 ]] || { echo "No vJunos-switch*.qcow2 in ~/images" >&2; exit 1; }
# vrnetlab n9kv requires the name n9kv-<version>.qcow2 (nexus9300v64.10.4.8.M -> n9kv-9300-10.4.8.M)
n9k_ver=$(basename "${n9k[0]}" .qcow2 | sed -E 's/^nexus9300v(64)?(-lite)?\.//')
cp "${n9k[0]}" ~/vrnetlab/cisco/n9kv/"n9kv-9300-${n9k_ver}.qcow2"
cp "${vjs[0]}" ~/vrnetlab/juniper/vjunosswitch/
# Let the topology choose vJunos vCPUs/RAM (VJUNOS_SMP / VJUNOS_RAM); upstream hard-codes 4 vCPUs.
python3 - <<'PY'
import pathlib
p = pathlib.Path.home() / "vrnetlab/juniper/vjunosswitch/docker/launch.py"
s = p.read_text()
if "VJUNOS_SMP" not in s:
    s = s.replace('ram=5120,', 'ram=int(os.getenv("VJUNOS_RAM", "5120")),')
    s = s.replace('smp="4,sockets=1,cores=4,threads=1",',
                  'smp="{0},sockets=1,cores={0},threads=1".format(os.getenv("VJUNOS_SMP", "4")),')
    p.write_text(s)
PY
(cd ~/vrnetlab/cisco/n9kv && make)
(cd ~/vrnetlab/juniper/vjunosswitch && make)
sudo docker images | grep -E 'n9kv|vjunos'
cat <<MSG

Images built. Make the tags above match lab/gen_topology.py (KINDS), regenerate, then:
  python lab/gen_topology.py > lab/topology.clab.yml
  sudo containerlab deploy -t lab/topology.clab.yml
  sudo containerlab inspect -t lab/topology.clab.yml     # wait until all nodes are 'healthy' (NX-OS: ~10 min)
MSG
