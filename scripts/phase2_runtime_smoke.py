import hashlib
import json
import sys
import uuid

import httpx
import psycopg

DATABASE_URL = "postgresql://brain:brain@localhost:5432/company_brain"
API_URL = "http://127.0.0.1:8010"


def main() -> None:
    connection = psycopg.connect(DATABASE_URL)
    organization_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = f"phase2-{uuid.uuid4()}"
    slug = f"phase2-runtime-{organization_id.hex[:8]}"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO organizations(id,name,slug,created_at,updated_at) "
                "VALUES(%s,'Phase2 Runtime',%s,now(),now())",
                (organization_id, slug),
            )
            cursor.execute(
                "INSERT INTO workspaces("
                "id,organization_id,name,slug,settings,created_at,updated_at) "
                "VALUES(%s,%s,'Knowledge','knowledge','{}',now(),now())",
                (workspace_id, organization_id),
            )
            cursor.execute(
                "INSERT INTO users("
                "id,organization_id,email,display_name,api_token_hash,created_at,updated_at) "
                "VALUES(%s,%s,%s,'Runtime',%s,now(),now())",
                (
                    user_id,
                    organization_id,
                    f"{slug}@example.com",
                    hashlib.sha256(token.encode()).hexdigest(),
                ),
            )
            cursor.execute(
                "INSERT INTO memberships("
                "id,organization_id,workspace_id,user_id,role,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,'editor',now(),now())",
                (uuid.uuid4(), organization_id, workspace_id, user_id),
            )
        connection.commit()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Organization-ID": str(organization_id),
            "X-Workspace-ID": str(workspace_id),
        }
        with httpx.Client(base_url=API_URL, headers=headers, timeout=10) as client:
            client.get("/health").raise_for_status()
            source = client.post(
                "/api/v1/documents",
                json={
                    "path": "review.md",
                    "markdown": "---\ntitle: Customer ABC Review\ntags: [risk]\n---\n"
                    "Payment delay increased. See [[Payment Policy]].",
                },
            )
            source.raise_for_status()
            target = client.post(
                "/api/v1/documents",
                json={
                    "path": "policy.md",
                    "markdown": "---\ntitle: Payment Policy\ntags: [policy]\n---\nApproved.",
                },
            )
            target.raise_for_status()
            search = client.get("/api/v1/search", params={"q": "payment delay", "tag": "risk"})
            search.raise_for_status()
            backlinks = client.get(
                f"/api/v1/documents/{target.json()['id']}/backlinks"
            )
            backlinks.raise_for_status()
            print(
                json.dumps(
                    {
                        "search_titles": [item["title"] for item in search.json()],
                        "backlink_titles": [item["title"] for item in backlinks.json()],
                    }
                )
            )
    finally:
        connection.rollback()
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"runtime smoke failed: {error}", file=sys.stderr)
        raise
