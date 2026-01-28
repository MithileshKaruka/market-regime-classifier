#!/bin/bash
# EC2 Instance Setup Script
# Run on a fresh Amazon Linux 2023 or Ubuntu 22.04 EC2 instance

set -e

echo "=== Installing Docker ==="
sudo yum update -y 2>/dev/null || sudo apt-get update -y
sudo yum install -y docker 2>/dev/null || sudo apt-get install -y docker.io
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker $USER

echo "=== Installing Docker Compose ==="
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

echo "=== Installing Git ==="
sudo yum install -y git 2>/dev/null || sudo apt-get install -y git

echo ""
echo "=== Setup Complete ==="
echo "Log out and back in for docker group permissions to take effect."
echo "Then run: git clone <your-repo> && cd market-regime-classifier"
