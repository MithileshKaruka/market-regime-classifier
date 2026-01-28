# AWS Deployment Guide - EC2 with Docker Compose

Deploy the Market Regime Classifier to AWS EC2 using Docker Compose.

## Prerequisites

- AWS account with EC2 access
- SSH key pair for EC2 access
- Databento API key

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    EC2 Instance                          │
│                   (t3.medium)                           │
│                                                          │
│  ┌─────────────────┐    ┌─────────────────┐            │
│  │    Frontend     │    │     Backend     │            │
│  │   (nginx:80)    │───▶│  (FastAPI:8000) │            │
│  └─────────────────┘    └────────┬────────┘            │
│                                  │                      │
│                         ┌────────▼────────┐            │
│                         │     DuckDB      │            │
│                         │  (Persistent)   │            │
│                         └─────────────────┘            │
└─────────────────────────────────────────────────────────┘
```

## Code Changes Required

### 1. Make Frontend API URL Configurable

**File:** `frontend/src/config.ts`

```typescript
// Change line 8 from:
baseUrl: 'http://127.0.0.1:8000',

// To:
baseUrl: import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000',
```

### 2. Update Frontend Dockerfile

**File:** `frontend/Dockerfile`

```dockerfile
# Build stage
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# Build arg for API URL (defaults to localhost for dev)
ARG VITE_API_URL=http://127.0.0.1:8000
ENV VITE_API_URL=$VITE_API_URL

# Build the app
RUN npm run build

# Production stage
FROM nginx:alpine

# Copy built files to nginx
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Expose port 80
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
```

### 3. Create Production Docker Compose

**File:** `docker-compose.prod.yml`

```yaml
version: '3.8'

services:
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    volumes:
      - backend-data:/app/data
    env_file:
      - ./backend/.env
    environment:
      - PYTHONUNBUFFERED=1
      - LOG_LEVEL=INFO
      - ENVIRONMENT=production
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
      args:
        - VITE_API_URL=${VITE_API_URL:-http://localhost:8000}
    ports:
      - "80:80"
    depends_on:
      - backend
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  backend-data:
    driver: local
```

### 4. Create EC2 Setup Script

**File:** `scripts/setup-ec2.sh`

```bash
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
```

### 5. Create Config Transfer Script

**File:** `scripts/transfer-config.sh`

Run this from your local machine to transfer config files to EC2:

```bash
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
fi

# Transfer secrets.yaml
if [ -f "backend/config/secrets.yaml" ]; then
    echo "  Transferring backend/config/secrets.yaml"
    scp -i "$KEY_PATH" backend/config/secrets.yaml ${EC2_USER}@${EC2_IP}:${REMOTE_PATH}/backend/config/
fi

echo ""
echo "Config files transferred successfully!"
echo "Now SSH to EC2 and run: ./scripts/deploy-aws.sh"
```

### 6. Create Deployment Script

**File:** `scripts/deploy-aws.sh`

```bash
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
```

### 6. Create Production Environment Template

**File:** `backend/.env.production.example`

```bash
# ==============================================
# Production Environment Configuration
# ==============================================

# Required: Databento API Key
DATABENTO_API_KEY=your_databento_api_key_here

# Optional: LLM API Keys (for agent features)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Application Settings
LOG_LEVEL=INFO
ENVIRONMENT=production

# Storage (local for EC2 with EBS)
STORAGE_BACKEND=local
LOCAL_STORAGE_PATH=/app/data
```

---

## EC2 Deployment Steps

### Step 1: Launch EC2 Instance

1. Go to **EC2 Dashboard** → **Launch Instance**

2. Configure:
   - **Name:** `market-regime-classifier`
   - **AMI:** Amazon Linux 2023 or Ubuntu 22.04
   - **Instance Type:** `t3.medium` (2 vCPU, 4 GB RAM)
   - **Key Pair:** Select or create one
   - **Storage:** 50 GB gp3

3. **Security Group** - Create new with rules:
   | Type | Port | Source | Description |
   |------|------|--------|-------------|
   | SSH | 22 | Your IP | SSH access |
   | HTTP | 80 | 0.0.0.0/0 | Frontend |
   | Custom TCP | 8000 | 0.0.0.0/0 | Backend API |

4. Click **Launch Instance**

### Step 2: Connect and Setup

```bash
# SSH to your instance
ssh -i your-key.pem ec2-user@<ec2-public-ip>

# Download and run setup script
curl -fsSL https://raw.githubusercontent.com/<your-repo>/main/scripts/setup-ec2.sh | bash

# IMPORTANT: Log out and back in for docker permissions
exit
ssh -i your-key.pem ec2-user@<ec2-public-ip>
```

### Step 3: Clone Repository

```bash
# Clone the repository
git clone https://github.com/<your-username>/market-regime-classifier.git
cd market-regime-classifier
```

### Step 4: Transfer Config Files from Local Machine

Run these commands **from your local machine** (not EC2) to transfer your existing config files:

```bash
# Transfer .env file
scp -i your-key.pem backend/.env ec2-user@<ec2-ip>:~/market-regime-classifier/backend/

# Transfer secrets.yaml (if exists)
scp -i your-key.pem backend/config/secrets.yaml ec2-user@<ec2-ip>:~/market-regime-classifier/backend/config/
```

**Alternative:** Create config files manually on EC2:
```bash
# On EC2, create .env from template
cp backend/.env.production.example backend/.env
nano backend/.env  # Add your Databento API key
```

### Step 5: Deploy

Run these commands **on EC2**:

```bash
# Make deploy script executable
chmod +x scripts/deploy-aws.sh

# Deploy (auto-detects EC2 public IP)
./scripts/deploy-aws.sh
```

### Step 6: Verify Deployment

```bash
# Check container status
docker-compose -f docker-compose.prod.yml ps

# View logs
docker-compose -f docker-compose.prod.yml logs -f

# Test health endpoint
curl http://localhost:8000/api/health
```

Open `http://<ec2-public-ip>` in your browser to access the frontend.

---

## Managing the Deployment

### View Logs

```bash
# All services
docker-compose -f docker-compose.prod.yml logs -f

# Backend only
docker-compose -f docker-compose.prod.yml logs -f backend

# Frontend only
docker-compose -f docker-compose.prod.yml logs -f frontend
```

### Restart Services

```bash
# Restart all
docker-compose -f docker-compose.prod.yml restart

# Restart backend only
docker-compose -f docker-compose.prod.yml restart backend
```

### Update Deployment

```bash
# Pull latest code
git pull

# Rebuild and restart
docker-compose -f docker-compose.prod.yml build
docker-compose -f docker-compose.prod.yml up -d
```

### Stop Services

```bash
docker-compose -f docker-compose.prod.yml down
```

### Backup Data

```bash
# Backup DuckDB database
docker cp $(docker-compose -f docker-compose.prod.yml ps -q backend):/app/data/market_data.duckdb ./backup/
```

---

## Preloading Historical Data

After deployment, preload historical data:

```bash
# SSH into the backend container
docker-compose -f docker-compose.prod.yml exec backend bash

# Estimate cost first
python scripts/data/preload_historical.py --estimate

# Preload data (5yr OHLCV + 60d MBP-1)
python scripts/data/preload_historical.py --load
```

---

## Cost Estimate

| Resource | Monthly Cost |
|----------|--------------|
| t3.medium (on-demand) | ~$30 |
| 50 GB gp3 storage | ~$4 |
| Data transfer (minimal) | ~$1-5 |
| **Total** | **~$35-40** |

**Cost Optimization:**
- Use Reserved Instances (1-year) for ~40% savings
- Use Spot Instances for non-production (~70% savings)
- Stop instance when not in use

---

## Troubleshooting

### Frontend shows "Cannot connect to API"

1. Check backend is running: `docker-compose -f docker-compose.prod.yml ps`
2. Verify security group allows port 8000
3. Check API URL was set correctly during build:
   ```bash
   docker-compose -f docker-compose.prod.yml logs frontend | grep VITE
   ```

### Backend container keeps restarting

```bash
# Check logs for errors
docker-compose -f docker-compose.prod.yml logs backend

# Common issues:
# - Missing .env file or DATABENTO_API_KEY
# - Port 8000 already in use
```

### Out of disk space

```bash
# Check disk usage
df -h

# Clean up Docker resources
docker system prune -a

# Check data directory size
du -sh /var/lib/docker/volumes/
```

### Database connection issues

```bash
# Verify data volume exists
docker volume ls

# Check database file
docker-compose -f docker-compose.prod.yml exec backend ls -la /app/data/
```
