"""Subscription and billing endpoints with Razorpay integration."""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from decimal import Decimal, ROUND_HALF_UP

import requests
from flask import Blueprint, current_app, jsonify, render_template, request, session, url_for
from flask_login import current_user, login_required

from ..extensions import csrf, db
from ..models import OrganizationSubscription, PricingPlan, SeatPurchase
from ..tenant.utils import admin_required, tenant_required

billing_bp = Blueprint("billing", __name__, template_folder="../templates/billing")


def _decimal(value: float | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _default_plan() -> PricingPlan:
    code = current_app.config.get("DEFAULT_PLAN_CODE", "CLINKER_PRO_INDIA")
    plan = PricingPlan.query.filter_by(code=code).first()
    if plan:
        return plan

    plan = PricingPlan(
        code=code,
        name="Clinker Pro India",
        currency="INR",
        base_amount=_decimal(current_app.config.get("PRICING_BASE_AMOUNT_INR", 9999)),
        per_seat_amount=_decimal(current_app.config.get("PRICING_PER_SEAT_INR", 499)),
        is_active=True,
    )
    db.session.add(plan)
    db.session.commit()
    return plan


def _subscription(org_id: int) -> OrganizationSubscription:
    plan = _default_plan()
    subscription = OrganizationSubscription.bootstrap(org_id, plan)
    subscription.refresh_status()
    db.session.commit()
    return subscription


def _seat_summary(subscription: OrganizationSubscription) -> dict:
    return {
        "status": subscription.status,
        "seat_limit": subscription.seat_limit,
        "remaining_seats": subscription.remaining_seats,
        "paid_seats": subscription.paid_seats,
    }


def _compute_amounts(plan: PricingPlan, seats: int, charge_base: bool = True) -> dict[str, Decimal]:
    base_amount = _decimal(plan.base_amount) if charge_base else _decimal(0)
    per_seat_amount = _decimal(plan.per_seat_amount)
    subtotal = base_amount + (per_seat_amount * seats)
    gst_rate = _decimal(current_app.config.get("GST_RATE", 0.18))
    tax_amount = (subtotal * gst_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total = subtotal + tax_amount
    return {
        "base": base_amount,
        "per_seat": per_seat_amount,
        "subtotal": subtotal,
        "tax": tax_amount,
        "total": total,
    }


def _razorpay_auth() -> tuple[str, str] | None:
    key_id = current_app.config.get("RAZORPAY_KEY_ID")
    key_secret = current_app.config.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    return key_id, key_secret


def _create_razorpay_order(amount: Decimal, currency: str, receipt: str, notes: dict) -> dict:
    auth = _razorpay_auth()
    if not auth:
        raise RuntimeError("Razorpay keys not configured")

    payload = {
        "amount": int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP)),
        "currency": currency,
        "receipt": receipt,
        "notes": notes,
        "payment_capture": 1,
    }
    response = requests.post(
        "https://api.razorpay.com/v1/orders",
        auth=auth,
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = current_app.config.get("RAZORPAY_WEBHOOK_SECRET") or ""
    if not signature or not secret:
        return False
    computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def _verify_payment_signature(order_id: str, payment_id: str, signature: str | None) -> bool:
    secret = current_app.config.get("RAZORPAY_KEY_SECRET") or ""
    if not secret or not signature:
        return False
    message = f"{order_id}|{payment_id}".encode()
    computed = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


@billing_bp.route("/status", methods=["GET"])
@login_required
@tenant_required
@admin_required
def subscription_status():
    org_id = session.get("org_id") or current_user.organization_id
    subscription = _subscription(org_id)
    plan = subscription.plan or _default_plan()
    data = {
        "status": subscription.status,
        "trial_ends_at": subscription.trial_ends_at.isoformat(),
        "seat_limit": subscription.seat_limit,
        "remaining_seats": subscription.remaining_seats,
        "paid_seats": subscription.paid_seats,
        "plan": {
            "code": plan.code,
            "name": plan.name,
            "currency": plan.currency,
            "base": str(plan.base_amount),
            "per_seat": str(plan.per_seat_amount),
        },
    }
    return jsonify(data)


@billing_bp.route("/upgrade", methods=["GET"])
@login_required
@tenant_required
@admin_required
def upgrade_page():
    org_id = session.get("org_id") or current_user.organization_id
    subscription = _subscription(org_id)
    plan = subscription.plan or _default_plan()
    has_paid_purchase = subscription.paid_seats > 0 or SeatPurchase.query.filter_by(organization_id=org_id, status="paid").count() > 0
    activation_charge = plan.base_amount if not has_paid_purchase else _decimal(0)
    purchases = (
        SeatPurchase.query.filter_by(organization_id=org_id)
        .order_by(SeatPurchase.created_at.desc())
        .limit(10)
        .all()
    )
    return render_template(
        "billing/upgrade.html",
        subscription=subscription,
        plan=plan,
        razorpay_key=current_app.config.get("RAZORPAY_KEY_ID", ""),
        gst_rate=current_app.config.get("GST_RATE", 0.18),
        purchases=purchases,
        activation_charge=activation_charge,
        charge_base=not has_paid_purchase,
    )


@billing_bp.route("/order", methods=["POST"])
@login_required
@tenant_required
@admin_required
def create_order():
    org_id = session.get("org_id") or current_user.organization_id
    payload = request.get_json(silent=True) or {}
    seats = int(payload.get("seats") or 0)
    if seats <= 0:
        return jsonify({"error": "Seat quantity must be greater than zero."}), 400

    subscription = _subscription(org_id)
    plan = subscription.plan or _default_plan()
    has_paid_purchase = subscription.paid_seats > 0 or SeatPurchase.query.filter_by(organization_id=org_id, status="paid").count() > 0
    amounts = _compute_amounts(plan, seats, charge_base=not has_paid_purchase)

    purchase = SeatPurchase(
        organization_id=org_id,
        plan_id=plan.id,
        seats_purchased=seats,
        currency=plan.currency,
        base_amount=amounts["base"],
        per_seat_amount=amounts["per_seat"],
        amount_subtotal=amounts["subtotal"],
        tax_amount=amounts["tax"],
        amount_total=amounts["total"],
        status="created",
    )
    db.session.add(purchase)
    db.session.flush()

    receipt = f"org-{org_id}-purchase-{purchase.id}-{uuid.uuid4().hex[:8]}"
    try:
        order = _create_razorpay_order(amounts["total"], plan.currency, receipt, {"org_id": org_id, "purchase_id": purchase.id})
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Failed to create Razorpay order")
        db.session.rollback()
        return jsonify({"error": f"Unable to initiate payment: {exc}"}), 502

    purchase.razorpay_order_id = order.get("id")
    purchase.status = "pending"
    db.session.commit()

    return jsonify(
        {
            "orderId": order.get("id"),
            "amount": order.get("amount"),
            "currency": order.get("currency"),
            "key": current_app.config.get("RAZORPAY_KEY_ID"),
            "purchaseId": purchase.id,
        }
    )


@billing_bp.route("/verify", methods=["POST"])
@login_required
@tenant_required
@admin_required
def verify_payment():
    org_id = session.get("org_id") or current_user.organization_id
    payload = request.get_json(silent=True) or {}
    order_id = payload.get("orderId")
    payment_id = payload.get("paymentId")
    signature = payload.get("signature")

    if not order_id or not payment_id or not signature:
        return jsonify({"error": "Missing payment verification fields."}), 400

    purchase = SeatPurchase.query.filter_by(razorpay_order_id=order_id, organization_id=org_id).first()
    if purchase is None:
        return jsonify({"error": "Purchase not found for this order."}), 404

    if purchase.status == "paid":
        subscription = _subscription(org_id)
        return jsonify({"ok": True, "seatSummary": _seat_summary(subscription)})

    if not _verify_payment_signature(order_id, payment_id, signature):
        return jsonify({"error": "Invalid payment signature."}), 400

    purchase.mark_paid(payment_id=payment_id, signature=signature)

    subscription = _subscription(purchase.organization_id)
    subscription.apply_payment(purchase.seats_purchased)

    db.session.commit()

    return jsonify({
        "ok": True,
        "seatSummary": _seat_summary(subscription),
        "purchase": {
            "id": purchase.id,
            "seats": purchase.seats_purchased,
            "total": str(purchase.amount_total),
            "status": purchase.status,
        },
    })


@billing_bp.route("/razorpay/webhook", methods=["POST"])
@csrf.exempt
def razorpay_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature")

    if not _verify_webhook_signature(raw_body, signature):
        current_app.logger.warning("Invalid Razorpay signature")
        return ("invalid signature", 400)

    payload = request.get_json(force=True, silent=True) or {}
    event = payload.get("event") or ""
    payment_entity = (payload.get("payload", {}).get("payment", {}) or {}).get("entity", {})
    order_id = payment_entity.get("order_id")
    payment_id = payment_entity.get("id")

    if not order_id:
        return ("missing order id", 400)

    purchase = SeatPurchase.query.filter_by(razorpay_order_id=order_id).first()
    if purchase is None:
        current_app.logger.warning("Webhook for unknown order %s", order_id)
        return ("unknown order", 200)

    if purchase.status == "paid":
        return ("already processed", 200)

    if event not in {"payment.captured", "payment.authorized"}:
        current_app.logger.info("Ignoring event %s for order %s", event, order_id)
        return ("ignored", 200)

    purchase.mark_paid(payment_id=payment_id or "unknown", signature=signature, payload=payload)

    subscription = _subscription(purchase.organization_id)
    subscription.apply_payment(purchase.seats_purchased)

    db.session.commit()

    return ("ok", 200)
