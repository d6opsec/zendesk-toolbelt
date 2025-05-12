import os
import httpx
from typing import Dict, Any, List
from httpx import BasicAuth
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv


load_dotenv()

app = FastAPI()

ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL")
ZENDESK_TOKEN = os.getenv("ZENDESK_TOKEN")
ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN")
TEST_EMAIL = os.getenv("TEST_EMAIL")

if not ZENDESK_EMAIL or not ZENDESK_TOKEN or not ZENDESK_SUBDOMAIN:
    raise ValueError("ZENDESK_EMAIL, ZENDESK_TOKEN, and ZENDESK_SUBDOMAIN must be set")

ZENDESK_API_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
auth = BasicAuth(f"{ZENDESK_EMAIL}/token", ZENDESK_TOKEN)


def authorize(request: Request):
    if not request.headers.get("authorization") == ZENDESK_TOKEN:
        print("Authorization header:", request.headers.get("authorization"))
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_ticket(ticket_id: int) -> Dict[str, Any]:
    """Get ticket details from Zendesk API"""
    with httpx.Client() as client:
        response = client.get(f"{ZENDESK_API_URL}/tickets/{ticket_id}", headers=headers, auth=auth)
        response.raise_for_status()
        return response.json()["ticket"]


def get_user_tickets(requester_id: int, sort_by: str = "created_at", sort_order: str = "asc") -> List[Dict[str, Any]]:
    """Get all tickets for a specific user"""
    tickets = []

    params = {"sort_by": sort_by, "sort_order": sort_order}
    with httpx.Client() as client:
        response = client.get(
            f"{ZENDESK_API_URL}/users/{requester_id}/tickets/requested",
            headers=headers,
            auth=auth,
            params=params,
        )
        response.raise_for_status()

        response_json = response.json()
        tickets.extend(response_json["tickets"])

        while response_json["next_page"]:
            response = client.get(response_json["next_page"], headers=headers, auth=auth)
            response.raise_for_status()

            response_json = response.json()
            tickets.extend(response_json["tickets"])

    return tickets


def merge_tickets(
    target_id: int,
    ids: list[int],
    target_comment: str = "",
    source_comment: str = "",
    target_comment_is_public: bool = False,
    source_comment_is_public: bool = False,
) -> Dict[str, Any]:
    """Merge tickets into a target ticket"""
    data = {
        "ids": ids,
        "target_comment": target_comment,
        "source_comment": source_comment,
        "target_comment_is_public": target_comment_is_public,
        "source_comment_is_public": source_comment_is_public,
    }
    with httpx.Client() as client:
        response = client.post(
            f"{ZENDESK_API_URL}/tickets/{target_id}/merge",
            headers=headers,
            auth=auth,
            json=data,
        )
        response.raise_for_status()
        return response.json()


def update_ticket_status(ticket_id: int, status: str) -> Dict[str, Any]:
    """Update ticket status"""
    data = {"ticket": {"status": status}}
    with httpx.Client() as client:
        response = client.put(f"{ZENDESK_API_URL}/tickets/{ticket_id}", headers=headers, auth=auth, json=data)
        response.raise_for_status()
        return response.json()


def safety_check(ticket: Dict[str, Any]) -> bool:
    """Check if the ticket is safe to merge."""
    return ticket["via"]["source"]["from"]["address"] == TEST_EMAIL


@app.post("/merge-ticket-webhook")
async def merge_ticket_webhook(request: Request):
    authorize(request)

    body = await request.json()
    print(f"Received webhook request: {body}")

    ticket_detail = body.get("detail", {})
    requester_id = ticket_detail.get("requester_id")

    if requester_id:
        new_ticket = get_ticket(ticket_detail.get("id"))
        print(f"New ticket #{new_ticket['id']}")

        all_tickets = get_user_tickets(requester_id)
        all_tickets.append(new_ticket)

        active_tickets = [ticket for ticket in all_tickets if ticket["status"] != "closed"]
        print(f"Found {len(active_tickets)}/{len(all_tickets)} active tickets for user {requester_id}")

        if len(active_tickets) > 1:
            active_tickets.sort(key=lambda x: x["created_at"])
            oldest_ticket = active_tickets[0]

            target_status = "new" if oldest_ticket["status"] == "new" else "open"

            for ticket in active_tickets[1:]:
                print(f"Merging ticket #{ticket['id']} into #{oldest_ticket['id']}")
                job_status = merge_tickets(
                    target_id=oldest_ticket["id"],
                    ids=[ticket["id"]],
                    target_comment=f"Ticket #{ticket['id']} ({ticket['subject']}: {ticket['description']}) merged into this ticket",
                    source_comment=f"Merged into ticket #{oldest_ticket['id']}",
                )

            updated_ticket = update_ticket_status(oldest_ticket["id"], target_status)
            print(f"Updated ticket #{oldest_ticket['id']} to '{target_status}'")
            return {"status": "success"}
        else:
            print("No tickets to merge")

    return {"status": "bypass"}


@app.get("/health-check")
async def health_check():
    return {"status": "ok"}
