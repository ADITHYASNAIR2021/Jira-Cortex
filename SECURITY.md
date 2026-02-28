# Jira Cortex - Security Policy

## Overview

Jira Cortex is designed with **security-first principles**. This document outlines the security measures implemented and guidelines for deployment.

## Authentication & Authorization

### JWT Token Validation

- All API endpoints require valid Atlassian JWT tokens
- Tokens are validated using Atlassian's public JWKS endpoint
- Token expiry is enforced with 60-second clock skew tolerance
- Timing attacks are prevented using constant-time string comparison

### Access Control Lists (ACL)

- Every document is tagged with `tenant_id` and `project_id`
- Searches are **always** filtered by user's project access
- Cache keys include ACL context to prevent cross-user leakage
- Users can only see data from projects they have access to in Jira

## Data Protection

### Secret Detection

- Uses `detect-secrets` library for robust secret detection
- Detects: AWS keys, GitHub tokens, private keys, JWTs, etc.
- Secrets are masked before storage with type-specific markers
- Additional patterns for Atlassian-specific tokens

### Data Sanitization

- HTML is stripped from descriptions and comments
- Control characters are removed from queries
- Input validation on all API endpoints (Pydantic)

## Infrastructure Security

### Network Security

- CORS restricted to Atlassian domains only
- Rate limiting (60 requests/minute by default)
- HTTPS enforced via Strict-Transport-Security header

### Security Headers

All responses include:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Strict-Transport-Security: max-age=31536000`
- `Cache-Control: no-store`

## Secure Development

### Environment Variables

- All secrets stored in environment variables
- `.env` file excluded from version control
- Production uses secrets manager (recommended)

### Dependencies

- All dependencies pinned to specific versions
- Regular security audits recommended
- No known vulnerabilities at time of release

## Deployment Checklist

Before deploying to production:

1. [ ] Replace `.env.example` values with production secrets
2. [ ] Update `CORS_ALLOWED_ORIGINS` with your domain
3. [ ] Update Forge manifest with production backend URL
4. [ ] Enable usage tracking for billing
5. [ ] Set `APP_DEBUG=false`
6. [ ] Configure Redis with password authentication
7. [ ] Use Qdrant Cloud with API key authentication
8. [ ] Set up monitoring and alerting
9. [ ] Review and test all ACL filtering logic

## Reporting Security Issues

Please report security vulnerabilities to: <security@your-domain.com>

Do NOT open public issues for security vulnerabilities.
