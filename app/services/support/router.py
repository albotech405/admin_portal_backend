from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
from uuid import uuid4

from app.core.dependencies import require_admin
from app.core.supabase import get_supabase

router = APIRouter(prefix="/admin/support", tags=["support"])


# ── Models ─────────────────────────────────────────────────────────────────────

class CreateTicketBody(BaseModel):
    user_id: str
    user_type: str = "customer"
    subject: str
    body: str
    priority: str = "normal"


class UpdateTicketBody(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    assigned_to: Optional[str] = None


class AddMessageBody(BaseModel):
    body: str
    sender_type: str = "admin"


class TicketMessageItem(BaseModel):
    id: str
    ticket_id: str
    sender_id: str
    sender_name: Optional[str] = None
    sender_type: str
    body: str
    created_at: str


class TicketListItem(BaseModel):
    id: str
    user_id: str
    user_name: Optional[str] = None
    user_phone: Optional[str] = None
    user_type: str
    subject: str
    status: str
    priority: str
    assigned_to: Optional[str] = None
    message_count: int = 0
    created_at: str
    updated_at: str


class TicketListResponse(BaseModel):
    tickets: List[TicketListItem]
    total: int


class UserContextItem(BaseModel):
    id: str
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    is_active: bool = True
    customer_rating: float = 0.0
    created_at: str


class TicketDetailResponse(BaseModel):
    id: str
    user_id: str
    user_type: str
    subject: str
    body: str
    status: str
    priority: str
    assigned_to: Optional[str] = None
    created_at: str
    updated_at: str
    messages: List[TicketMessageItem] = []
    user_context: Optional[UserContextItem] = None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/tickets", status_code=201)
def create_ticket(body: CreateTicketBody, _user=Depends(require_admin)):
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    ticket_id = str(uuid4())
    try:
        sb.table("support_tickets").insert({
            "id": ticket_id,
            "user_id": body.user_id,
            "user_type": body.user_type,
            "subject": body.subject,
            "body": body.body,
            "status": "open",
            "priority": body.priority,
            "created_at": now,
            "updated_at": now,
        }).execute()

        # Insert first message as opening body
        sb.table("ticket_messages").insert({
            "id": str(uuid4()),
            "ticket_id": ticket_id,
            "sender_id": _user.get("id", body.user_id),
            "sender_type": "admin",
            "body": body.body,
            "created_at": now,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"id": ticket_id, "message": "Ticket created"}


@router.get("/tickets", response_model=TicketListResponse)
def list_tickets(
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _user=Depends(require_admin),
):
    sb = get_supabase()
    try:
        query = sb.table("support_tickets").select("*")
        if status:
            query = query.eq("status", status)
        if priority:
            query = query.eq("priority", priority)

        count_query = sb.table("support_tickets").select("id", count="exact")
        if status:
            count_query = count_query.eq("status", status)
        if priority:
            count_query = count_query.eq("priority", priority)
        count_result = count_query.execute()
        total = count_result.count or 0

        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        rows = result.data or []

        # Enrich with user info
        user_ids = list({r["user_id"] for r in rows if r.get("user_id")})
        user_map = {}
        if user_ids:
            try:
                ur = sb.table("users").select("id, full_name, phone_number").in_("id", user_ids).execute()
                for u in ur.data or []:
                    user_map[u["id"]] = u
            except Exception:
                pass

        tickets = []
        for r in rows:
            uid = r.get("user_id", "")
            uinfo = user_map.get(uid, {})

            msg_count = 0
            try:
                mc = sb.table("ticket_messages").select("id", count="exact").eq("ticket_id", r["id"]).execute()
                msg_count = mc.count or 0
            except Exception:
                pass

            tickets.append(TicketListItem(
                id=r["id"],
                user_id=uid,
                user_name=uinfo.get("full_name"),
                user_phone=uinfo.get("phone_number"),
                user_type=r.get("user_type", "customer"),
                subject=r.get("subject", ""),
                status=r.get("status", "open"),
                priority=r.get("priority", "normal"),
                assigned_to=r.get("assigned_to"),
                message_count=msg_count,
                created_at=r.get("created_at", ""),
                updated_at=r.get("updated_at", ""),
            ))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return TicketListResponse(tickets=tickets, total=total)


@router.get("/tickets/{ticket_id}", response_model=TicketDetailResponse)
def get_ticket_detail(ticket_id: str, _user=Depends(require_admin)):
    sb = get_supabase()
    try:
        result = sb.table("support_tickets").select("*").eq("id", ticket_id).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.data:
        raise HTTPException(status_code=404, detail="Ticket not found")

    r = result.data

    # Fetch messages with sender name
    messages = []
    try:
        msg_result = (
            sb.table("ticket_messages")
            .select("*, users(full_name)")
            .eq("ticket_id", ticket_id)
            .order("created_at", asc=True)
            .execute()
        )
        for m in msg_result.data or []:
            uinfo = m.pop("users", {}) or {}
            messages.append(TicketMessageItem(
                id=m["id"],
                ticket_id=m["ticket_id"],
                sender_id=m["sender_id"],
                sender_name=uinfo.get("full_name"),
                sender_type=m.get("sender_type", "admin"),
                body=m["body"],
                created_at=m["created_at"],
            ))
    except Exception:
        pass

    # Fetch user context
    user_context = None
    try:
        ur = sb.table("users").select("*").eq("id", r["user_id"]).maybe_single().execute()
        if ur.data:
            u = ur.data
            user_context = UserContextItem(
                id=u["id"],
                full_name=u.get("full_name"),
                phone_number=u.get("phone_number"),
                email=u.get("email"),
                is_active=u.get("is_active", True),
                customer_rating=float(u.get("customer_rating", 0)),
                created_at=u.get("created_at", ""),
            )
    except Exception:
        pass

    return TicketDetailResponse(
        id=r["id"],
        user_id=r["user_id"],
        user_type=r.get("user_type", "customer"),
        subject=r.get("subject", ""),
        body=r.get("body", ""),
        status=r.get("status", "open"),
        priority=r.get("priority", "normal"),
        assigned_to=r.get("assigned_to"),
        created_at=r.get("created_at", ""),
        updated_at=r.get("updated_at", ""),
        messages=messages,
        user_context=user_context,
    )


@router.patch("/tickets/{ticket_id}")
def update_ticket(ticket_id: str, body: UpdateTicketBody, _user=Depends(require_admin)):
    sb = get_supabase()
    updates: dict = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if body.status is not None:
        updates["status"] = body.status
    if body.priority is not None:
        updates["priority"] = body.priority
    if body.assigned_to is not None:
        updates["assigned_to"] = body.assigned_to
    try:
        result = sb.table("support_tickets").update(updates).eq("id", ticket_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.data:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"message": "Ticket updated"}


@router.post("/tickets/{ticket_id}/messages", status_code=201)
def add_ticket_message(ticket_id: str, body: AddMessageBody, _user=Depends(require_admin)):
    sb = get_supabase()

    # Verify ticket exists
    try:
        ticket = sb.table("support_tickets").select("id, user_id").eq("id", ticket_id).maybe_single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not ticket.data:
        raise HTTPException(status_code=404, detail="Ticket not found")

    now = datetime.now(timezone.utc).isoformat()
    msg_id = str(uuid4())
    admin_id = _user.get("id", "")

    try:
        sb.table("ticket_messages").insert({
            "id": msg_id,
            "ticket_id": ticket_id,
            "sender_id": admin_id,
            "sender_type": body.sender_type,
            "body": body.body,
            "created_at": now,
        }).execute()

        # Update ticket updated_at
        sb.table("support_tickets").update({"updated_at": now}).eq("id", ticket_id).execute()

        # If admin replied, notify the user
        if body.sender_type == "admin":
            user_id = ticket.data.get("user_id")
            if user_id:
                sb.table("notifications").insert({
                    "id": str(uuid4()),
                    "user_id": user_id,
                    "notification_type": "support_reply",
                    "title": "Support reply received",
                    "content": body.body[:200],
                    "status": "unread",
                    "created_at": now,
                }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"id": msg_id, "message": "Message added"}
