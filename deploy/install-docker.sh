#!/usr/bin/env bash
# Install Docker Engine + the Compose plugin on Ubuntu (24.04 LTS), from
# Docker's own apt repository rather than the older distro package.
#
#   chmod +x deploy/install-docker.sh && ./deploy/install-docker.sh
#
# Also installs Nginx, which fronts the containers on :80/:443.
# Safe to re-run.

set -euo pipefail

echo "==> Updating apt and installing prerequisites"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl gnupg lsb-release

echo "==> Adding Docker's official GPG key and repository"
sudo install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
fi
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

echo "==> Installing Docker Engine, CLI, buildx and the Compose plugin"
sudo apt-get update -y
sudo apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "==> Installing Nginx (public reverse proxy)"
sudo apt-get install -y nginx

echo "==> Letting the 'ubuntu' user run docker without sudo"
sudo usermod -aG docker "${USER}"

echo "==> Enabling Docker and Nginx on boot"
sudo systemctl enable --now docker
sudo systemctl enable --now nginx

echo
echo "Installed:"
docker --version
sudo docker compose version
nginx -v 2>&1

cat <<'NOTE'

NEXT: log out and back in (or run `newgrp docker`) so the docker group
membership takes effect, then confirm WITHOUT sudo:

    docker --version
    docker compose version
    docker run --rm hello-world
NOTE
