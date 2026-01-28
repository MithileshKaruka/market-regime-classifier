#!/bin/bash
# Transfer config files to EC2
# Usage: ./scripts/transfer-config.sh <ec2-public-ip> <path-to-key.pem>

set -e

EC2_IP=$1
KEY_PATH=$2

if [ -z "$EC2_IP" ] || [ -z "$KEY_PATH" ]; then
    echo "Usage: ./scripts/transfer-config.sh <ec2-public-ip> <path-to-key.pem>"
    exit 1
fi

EC2_USER="ec2-user"
REMOTE_PATH="~/market-regime-classifier"

echo "Transferring config files to $EC2_IP..."

# Transfer .env
if [ -f "backend/.env" ]; then
    echo "  Transferring backend/.env"
    scp -i "$KEY_PATH" backend/.env ${EC2_USER}@${EC2_IP}:${REMOTE_PATH}/backend/
else
    echo "  Warning: backend/.env not found"
fi

# Transfer secrets.yaml
if [ -f "backend/config/secrets.yaml" ]; then
    echo "  Transferring backend/config/secrets.yaml"
    scp -i "$KEY_PATH" backend/config/secrets.yaml ${EC2_USER}@${EC2_IP}:${REMOTE_PATH}/backend/config/
else
    echo "  Warning: backend/config/secrets.yaml not found"
fi

echo ""
echo "Config files transferred successfully!"
echo "Now SSH to EC2 and run: ./scripts/deploy-aws.sh"
