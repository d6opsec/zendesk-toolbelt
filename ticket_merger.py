import os
import asyncio
import threading
import httpx
import time
import logging
from typing import Dict, Any, List
from httpx import BasicAuth
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
from contextlib import asynccontextmanager

logger = logging.getLogger("uvicorn")

load_dotenv()


HEALTH_CHECK_URL = os.environ["HEALTH_CHECK_URL"]
HEALTH_CHECK_INTERVAL = float(os.environ["HEALTH_CHECK_INTERVAL"])
PRE_DELAY = float(os.environ["PRE_DELAY"])


def keep_alive_worker():
    logger.info("Starting health check worker thread")

    async def ping():
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(HEALTH_CHECK_URL)
                response.raise_for_status()
        except Exception as e:
            logger.error(f"Health check failed: {e}")

    while True:
        asyncio.run(ping())
        time.sleep(HEALTH_CHECK_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    thread = threading.Thread(target=keep_alive_worker, daemon=True)
    thread.start()
    logger.info("Health check worker thread started")
    yield
    # Shutdown
    pass


app = FastAPI(lifespan=lifespan)

ZENDESK_EMAIL = os.environ["ZENDESK_EMAIL"]
ZENDESK_SUBDOMAIN = os.environ["ZENDESK_SUBDOMAIN"]
TEST_EMAIL = os.environ["TEST_EMAIL"]
ZENDESK_API_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}
auth = BasicAuth(f"{ZENDESK_EMAIL}/token", os.environ["ZENDESK_TOKEN"])


def authorize(request: Request):
    if not request.headers.get("authorization") == os.getenv("API_KEY"):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def get_ticket(ticket_id: int) -> Dict[str, Any]:
    """Get ticket details from Zendesk API"""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{ZENDESK_API_URL}/tickets/{ticket_id}", headers=headers, auth=auth)
        response.raise_for_status()
        return response.json()["ticket"]


async def get_user_tickets(
    requester_id: int, sort_by: str = "created_at", sort_order: str = "asc"
) -> dict[int, Dict[str, Any]]:
    """Get all tickets for a specific user using Zendesk Search API"""

    tickets: dict[int, Dict[str, Any]] = {}
    # Construct search query for tickets by requester
    query = f"type:ticket requester_id:{requester_id}"
    params: Dict[str, str | int] = {
        "query": query,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "per_page": "100",  # Maximum allowed by Zendesk
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{ZENDESK_API_URL}/search.json",
            headers=headers,
            auth=auth,
            params=params,
        )
        response.raise_for_status()

        response_json = response.json()
        tickets.update({ticket["id"]: ticket for ticket in response_json["results"]})

        # Handle pagination
        while response_json.get("next_page"):
            response = await client.get(response_json["next_page"], headers=headers, auth=auth)
            response.raise_for_status()

            response_json = response.json()
            tickets.update({ticket["id"]: ticket for ticket in response_json["results"]})

    return tickets


async def merge_tickets(
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
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{ZENDESK_API_URL}/tickets/{target_id}/merge",
            headers=headers,
            auth=auth,
            json=data,
        )
        response.raise_for_status()
        return response.json()


async def update_ticket_status(ticket_id: int, status: str) -> Dict[str, Any]:
    """Update ticket status"""
    data = {"ticket": {"status": status}}
    async with httpx.AsyncClient() as client:
        response = await client.put(f"{ZENDESK_API_URL}/tickets/{ticket_id}", headers=headers, auth=auth, json=data)
        response.raise_for_status()
        return response.json()


def safety_check(ticket: Dict[str, Any]) -> bool:
    """Check if the ticket is safe to merge."""
    return ticket["via"]["source"]["from"]["address"] == TEST_EMAIL


@app.post("/merge-ticket-webhook")
async def merge_ticket_webhook(request: Request):
    authorize(request)

    body = await request.json()
    logger.info(f"Received webhook request: {body}")

    ticket_detail = body.get("detail", {})
    requester_id = ticket_detail.get("requester_id")

    if requester_id:
        await asyncio.sleep(PRE_DELAY)
        new_ticket = await get_ticket(ticket_detail.get("id"))
        logger.info(f"New ticket #{new_ticket['id']}")

        all_tickets = await get_user_tickets(requester_id)
        all_tickets[new_ticket["id"]] = new_ticket

        active_tickets = {key: ticket for key, ticket in all_tickets.items() if ticket["status"] != "closed"}
        logger.info(f"Found {len(active_tickets)}/{len(all_tickets)} active tickets for user {requester_id}")

        if len(active_tickets) > 1:
            sorted_tickets = sorted(active_tickets.values(), key=lambda x: x["created_at"])
            oldest_ticket = sorted_tickets[0]

            target_status = "new" if oldest_ticket["status"] == "new" else "open"

            for ticket in sorted_tickets[1:]:
                logger.info(f"Merging ticket #{ticket['id']} into #{oldest_ticket['id']}")
                job_status = await merge_tickets(
                    target_id=oldest_ticket["id"],
                    ids=[ticket["id"]],
                    target_comment=f"Ticket #{ticket['id']} ({ticket['subject']}: {ticket['description']}) merged into this ticket",
                    source_comment=f"Merged into ticket #{oldest_ticket['id']}",
                )

                updated_ticket = await update_ticket_status(oldest_ticket["id"], target_status)
                logger.info(f"Updated ticket #{oldest_ticket['id']} to '{target_status}'")
            return {"status": "success"}
        else:
            logger.info("No tickets to merge")

    return {"status": "bypass"}


@app.get("/health")
async def health_check():
    return {"status": "ok"}
