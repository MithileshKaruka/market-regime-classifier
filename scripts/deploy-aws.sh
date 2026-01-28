#!/bin/bash
# Deploy to AWS EC2
# Usage: ./scripts/deploy-aws.sh [ec2-public-ip]

set -e

# Get EC2 public IP (auto-detect or use argument)
if [ -n "$1" ]; then
    EC2_IP=$1
else
    EC2_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "")
fi

if [ -z "$EC2_IP" ]; then
    echo "Error: Could not determine EC2 public IP"
    echo "Usage: ./scripts/deploy-aws.sh <ec2-public-ip>"
    exit 1
fi

export VITE_API_URL="http://${EC2_IP}:8000"

echo "============================================"
echo "  Deploying Market Regime Classifier"
echo "============================================"
echo "  API URL: $VITE_API_URL"
echo ""

# Build and deploy
echo "Building containers..."
docker-compose -f docker-compose.prod.yml build

echo "Starting services..."
docker-compose -f docker-compose.prod.yml up -d

echo ""
echo "============================================"
echo "  Deployment Complete!"
echo "============================================"
echo "  Frontend: http://${EC2_IP}"
echo "  Backend:  http://${EC2_IP}:8000/api/health"
echo ""
echo "  View logs: docker-compose -f docker-compose.prod.yml logs -f"
echo "============================================"
