"""
Jira Cortex - Payments Router

Stripe integration for the pre-paid wallet system.
Handles checkout sessions, webhooks, and balance management.
"""

import stripe
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional
import structlog

from app.config import get_settings
from app.services.billing import get_billing_service, BillingService
from app.auth.dependencies import get_current_user
from app.models.schemas import UserContext

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

# Initialize Stripe
settings = get_settings()
if settings.stripe_secret_key:
    stripe.api_key = settings.stripe_secret_key


# -----------------------------------------
# Request/Response Models
# -----------------------------------------

class CreateCheckoutRequest(BaseModel):
    """Request to create a Stripe checkout session."""
    amount: int = 100  # Dollar amount ($100 default)
    product_type: str = "credits"  # 'credits' or 'subscription'


class WalletResponse(BaseModel):
    """Wallet balance response."""
    tenant_id: str
    balance: float
    currency: str
    is_active: bool
    auto_recharge: bool


# -----------------------------------------
# Endpoints
# -----------------------------------------

@router.get("/wallet")
async def get_wallet(
    user: UserContext = Depends(get_current_user),
    billing_service: BillingService = Depends(get_billing_service)
) -> WalletResponse:
    """
    Get current wallet balance and status.
    """
    wallet = await billing_service.get_wallet(user.tenant_id)
    
    if not wallet:
        # Create wallet if doesn't exist
        wallet = await billing_service.create_wallet(user.tenant_id)
    
    return WalletResponse(
        tenant_id=wallet.tenant_id,
        balance=wallet.balance,
        currency=wallet.currency,
        is_active=wallet.is_active,
        auto_recharge=wallet.auto_recharge
    )


@router.post("/create-checkout-session")
async def create_checkout_session(
    request_body: CreateCheckoutRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Creates a Stripe Checkout page for the user to pay.
    
    Supports:
    - Credits purchase (one-time payment)
    - Platform subscription ($299/mo)
    """
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured. Contact support."
        )
    
    try:
        if request_body.product_type == "subscription":
            # Platform Fee ($299/mo subscription)
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price': settings.stripe_platform_price_id,  # Pre-configured in Stripe
                    'quantity': 1,
                }],
                mode='subscription',
                success_url=f"{settings.app_frontend_url}/admin/settings?success=subscription",
                cancel_url=f"{settings.app_frontend_url}/admin/settings?canceled=true",
                client_reference_id=user.tenant_id,
                metadata={
                    'tenant_id': user.tenant_id,
                    'type': 'subscription'
                }
            )
        else:
            # Credits purchase (one-time)
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {
                            'name': 'Jira Cortex AI Credits',
                            'description': f'${request_body.amount} in AI processing credits',
                        },
                        'unit_amount': request_body.amount * 100,  # Convert to cents
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=f"{settings.app_frontend_url}/admin/settings?success=credits",
                cancel_url=f"{settings.app_frontend_url}/admin/settings?canceled=true",
                client_reference_id=user.tenant_id,
                metadata={
                    'tenant_id': user.tenant_id,
                    'type': 'credits',
                    'amount': str(request_body.amount)
                }
            )
        
        logger.info(
            "checkout_session_created",
            tenant_id=user.tenant_id,
            session_id=session.id,
            type=request_body.product_type
        )
        
        return {"url": session.url, "session_id": session.id}
        
    except stripe.error.StripeError as e:
        logger.error("stripe_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    billing_service: BillingService = Depends(get_billing_service)
):
    """
    Stripe webhook endpoint.
    
    Handles:
    - checkout.session.completed: Add credits or activate subscription
    - invoice.paid: Subscription renewal
    - customer.subscription.deleted: Subscription cancelled
    """
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook not configured")
    
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except ValueError:
        logger.error("webhook_invalid_payload")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        logger.error("webhook_invalid_signature")
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    event_type = event['type']
    data = event['data']['object']
    
    logger.info("stripe_webhook_received", event_type=event_type)
    
    # Handle checkout completion
    if event_type == 'checkout.session.completed':
        tenant_id = data.get('client_reference_id')
        metadata = data.get('metadata', {})
        
        if metadata.get('type') == 'subscription':
            # Activate platform subscription
            subscription_id = data.get('subscription')
            await billing_service.activate_subscription(
                tenant_id=tenant_id,
                subscription_id=subscription_id
            )
            logger.info("subscription_activated", tenant_id=tenant_id)
            
        else:
            # Add credits
            amount_cents = data.get('amount_total', 0)
            amount_dollars = amount_cents / 100.0
            
            await billing_service.add_credits(
                tenant_id=tenant_id,
                amount=amount_dollars,
                stripe_session_id=data.get('id')
            )
            logger.info(
                "credits_added",
                tenant_id=tenant_id,
                amount=amount_dollars
            )
    
    # Handle subscription renewal
    elif event_type == 'invoice.paid':
        subscription_id = data.get('subscription')
        if subscription_id:
            # Extend subscription
            customer_id = data.get('customer')
            # Look up tenant by Stripe customer ID
            await billing_service.extend_subscription(subscription_id)
    
    # Handle subscription cancellation
    elif event_type == 'customer.subscription.deleted':
        subscription_id = data.get('id')
        await billing_service.deactivate_subscription(subscription_id)
        logger.warning("subscription_cancelled", subscription_id=subscription_id)
    
    return {"status": "success"}


@router.get("/transactions")
async def get_transactions(
    user: UserContext = Depends(get_current_user),
    billing_service: BillingService = Depends(get_billing_service),
    limit: int = 50
):
    """
    Get recent wallet transactions for the tenant.
    """
    transactions = await billing_service.get_transactions(
        tenant_id=user.tenant_id,
        limit=limit
    )
    
    return {
        "transactions": [
            {
                "id": t.id,
                "amount": t.amount,
                "type": t.type,
                "description": t.description,
                "status": t.status,
                "created_at": t.created_at.isoformat()
            }
            for t in transactions
        ]
    }


@router.post("/estimate-cost")
async def estimate_sync_cost(
    issue_count: int,
    user: UserContext = Depends(get_current_user),
    billing_service: BillingService = Depends(get_billing_service)
):
    """
    Estimate the cost of syncing a given number of issues.
    
    Used by Admin UI to show cost before starting sync.
    """
    # Approximate tokens per issue (summary + description + comments)
    avg_tokens_per_issue = 500
    total_tokens = issue_count * avg_tokens_per_issue
    
    # Get current pricing
    cost = billing_service.calculate_embedding_cost(total_tokens)
    balance = await billing_service.get_balance(user.tenant_id)
    
    return {
        "issue_count": issue_count,
        "estimated_tokens": total_tokens,
        "estimated_cost": round(cost, 2),
        "current_balance": round(balance, 2),
        "sufficient_funds": balance >= cost,
        "shortfall": round(max(0, cost - balance), 2)
    }
