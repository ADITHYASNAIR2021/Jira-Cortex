# Jira Cortex

Permission-aware AI intelligence layer for Atlassian Jira

Transform your Jira "dead data" into actionable insights with natural language queries, powered by RAG (Retrieval-Augmented Generation) with strict security controls.

## Features

- **Natural Language Queries** - Ask questions about your Jira issues in plain English
- **ACL-Filtered Search** - Users only see results from their accessible projects
- **Citation-Backed Answers** - Every answer includes source issue links
- **Confidence Scores** - Know how reliable each answer is (0-100%)
- **Real-Time Sync** - Webhook-based updates keep data fresh
- **Admin Dashboard** - Historic data sync and usage statistics
- **Usage Billing** - Token-level tracking for cost management

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- Docker & Docker Compose
- Atlassian Developer Account

### 1. Clone and Setup

```bash
git clone https://github.com/your-org/jira-cortex.git
cd jira-cortex
```

### 2. Start Infrastructure

```bash
docker-compose up -d
```

### 3. Configure Backend

```bash
cd backend
cp .env.example .env
# Edit .env with your API keys

# Run database migrations
alembic upgrade head

# Install dependencies
pip install -r requirements.txt

# Start server
uvicorn main:app --reload
```

### 4. Deploy Forge App

```bash
cd forge-app
npm install
forge deploy
forge install
```

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                     Atlassian Jira                          │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────────────────┐│
│  │Context Panel│ │ Omni Search │ │     Admin Settings      ││
│  │ (Issue View)│ │(Global Page)│ │ (Historic Sync + Stats) ││
│  └──────┬──────┘ └──────┬──────┘ └───────────┬─────────────┘│
│         │               │                     │              │
│         └───────────────┼─────────────────────┘              │
│                         │                                    │
│                    Forge App                                 │
│                    (Node.js)                                 │
└─────────────────────────┬───────────────────────────────────┘
                          │ HTTPS + JWT
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    Python Backend                           │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ Security Middleware: Rate Limiting + JWT + CORS         ││
│  └─────────────────────────────────────────────────────────┘│
│  ┌──────────────┬──────────────┬──────────────────────────┐ │
│  │  /query      │  /ingest/*   │  /usage/current          │ │
│  │  (RAG)       │  (Batch/RT)  │  (Billing Stats)         │ │
│  └──────┬───────┴──────┬───────┴──────────────────────────┘ │
│         │              │                                    │
│  ┌──────┴───────┬──────┴───────┬──────────────────────────┐ │
│  │   Vector     │     LLM      │       Billing            │ │
│  │   Store      │   Service    │       Service            │ │
│  │   (Qdrant)   │  (OpenAI)    │     (PostgreSQL)         │ │
│  └──────────────┴──────────────┴──────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## API Endpoints

### Query

```http
POST /api/v1/query
Authorization: Bearer <forge-jwt>
Content-Type: application/json

{
  "query": "How did we fix the payment timeout issue?",
  "context": { "current_issue_key": "PAY-123" }
}
```

Response:

```json
{
  "answer": "The payment timeout was fixed by increasing the Stripe webhook timeout from 10s to 30s. [Issue-Key: PAY-456]",
  "confidence_score": 85.0,
  "citations": [
    {
      "issue_key": "PAY-456",
      "title": "Fix Stripe webhook timeout",
      "url": "https://your-site.atlassian.net/browse/PAY-456",
      "relevance_score": 0.92
    }
  ],
  "tokens_used": 1250
}
```

### Ingest (Batch - Async)

```http
POST /api/v1/ingest/batch
Authorization: Bearer <forge-jwt>

{
  "issues": [...],
  "tenant_id": "your-site"
}
```

Returns `202 Accepted` immediately with `job_id` for status polling.

### Rate Limits

| Endpoint | Limit |
| -------- | ----- |
| `/query` | 60/minute |
| `/ingest/batch` | 10/minute |
| `/ingest/single` | 120/minute |

## Security

See [SECURITY.md](./SECURITY.md) for complete security documentation.

**Key Security Features:**

- JWT validation with Atlassian JWKS
- ACL filtering on all vector searches
- Secret detection before embedding
- Rate limiting on all endpoints
- Tenant isolation at database level

## Billing

Jira Cortex tracks token usage for billing:

- All OpenAI API calls are metered
- Usage stored in PostgreSQL (`usage_records` table)
- Accessible via `/api/v1/usage/current` endpoint
- Admin dashboard shows monthly usage

**Database Migrations:**

```bash
# Create new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback last migration
alembic downgrade -1
```

## Environment Variables

| Variable | Required | Description |
| -------- | -------- | ----------- |
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `QDRANT_URL` | Yes | Qdrant vector DB URL |
| `REDIS_URL` | Yes | Redis cache URL |
| `DATABASE_URL` | Yes | PostgreSQL for billing |
| `JWT_SECRET_KEY` | Yes | 32+ char secret |
| `ATLASSIAN_CLIENT_ID` | Yes | Forge app client ID |

## Development

### Run Tests

```bash
cd backend
pytest tests/ -v --cov=app
```

### Local Development with Forge Tunnel

```bash
# Terminal 1: Backend
cd backend && uvicorn main:app --reload

# Terminal 2: Forge tunnel
cd forge-app && forge tunnel
```

## Production Deployment

### Docker Deployment

```bash
# Build production image (multi-stage, ~100MB)
cd backend
docker build -t jira-cortex:latest .

# Run with environment variables
docker run -d \
  -p 8000:8000 \
  -e OPENAI_API_KEY=sk-xxx \
  -e QDRANT_URL=http://qdrant:6333 \
  -e REDIS_URL=redis://redis:6379/0 \
  -e DATABASE_URL=postgresql://user:pass@db:5432/cortex \
  -e ALLOWED_TENANTS=your-tenant-cloud-id \
  -e APP_ENV=production \
  jira-cortex:latest
```

### Kubernetes Deployment

```bash
# Apply manifests
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secrets-template.yaml  # Update with real values first!
kubectl apply -f k8s/deployment.yaml

# Verify deployment
kubectl get pods -l app=jira-cortex
kubectl logs -l app=jira-cortex
```

### CI/CD (GitHub Actions)

The project includes GitHub Actions workflows:

- **`.github/workflows/ci.yml`** - Lint, test, build Docker image on every push
- **`.github/workflows/deploy.yml`** - Deploy to staging/production on tag push

Required GitHub secrets:

- `RENDER_API_KEY` / `RENDER_SERVICE_ID` (or your cloud provider)
- `FORGE_EMAIL` / `FORGE_API_TOKEN` (for Forge deployment)

---

## Monitoring & Observability

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/health` | Load balancer health check |
| `/ready` | Kubernetes readiness probe (503 if deps down) |
| `/metrics` | Prometheus-compatible metrics |
| `/api/v1/admin/status` | Detailed service status (auth required) |
| `/docs` | Swagger/OpenAPI documentation |
| `/redoc` | ReDoc documentation |

### Prometheus Metrics

```promql
# Total requests this month
jira_cortex_requests_total

# Total tokens consumed
jira_cortex_tokens_total

# Service up status
jira_cortex_up
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | ✅ | - | OpenAI API key |
| `QDRANT_URL` | ✅ | - | Qdrant vector DB URL |
| `REDIS_URL` | ✅ | - | Redis cache URL |
| `DATABASE_URL` | ⚠️ | - | PostgreSQL for billing (optional in dev) |
| `JWT_SECRET_KEY` | ✅ | - | 32+ character secret for JWT signing |
| `ATLASSIAN_CLIENT_ID` | ✅ | - | Forge app client ID |
| `ATLASSIAN_CLIENT_SECRET` | ✅ | - | Forge app client secret |
| `ALLOWED_TENANTS` | ⚠️ | - | Comma-separated tenant cloud IDs (empty in dev = allow all) |
| `APP_ENV` | ❌ | development | `development`, `staging`, or `production` |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | ❌ | 60 | API rate limit per user |

---

## Security

See [SECURITY.md](SECURITY.md) for:

- JWT validation and authentication
- Rate limiting configuration
- ACL-based data filtering
- Secret detection before embedding
- Tenant isolation

## License

Proprietary - All rights reserved.
