import base64
import hashlib
import json
import os
import sys
import uuid

import httpx
import psycopg

DATABASE_URL = os.getenv(
    "PHASE4_DATABASE_URL",
    "postgresql://brain:brain@localhost:5432/company_brain",
)
API_URL = os.getenv("PHASE4_API_URL", "http://127.0.0.1:8010")


def main() -> None:
    connection = psycopg.connect(DATABASE_URL)
    organization_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = f"phase4-{uuid.uuid4()}"
    slug = f"phase4-runtime-{organization_id.hex[:8]}"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO organizations(id,name,slug,created_at,updated_at) "
                "VALUES(%s,'Phase4 Runtime',%s,now(),now())",
                (organization_id, slug),
            )
            cursor.execute(
                "INSERT INTO workspaces("
                "id,organization_id,name,slug,settings,created_at,updated_at) "
                "VALUES(%s,%s,'Ingestion','ingestion','{}',now(),now())",
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
            csv_content = b"customer,amount\nRuntime Customer,1250\n"
            succeeded = client.post(
                "/api/v1/ingestions",
                json={
                    "source_type": "upload",
                    "uri": "upload://runtime.csv",
                    "filename": "runtime.csv",
                    "media_type": "text/csv",
                    "content_base64": base64.b64encode(csv_content).decode(),
                },
            )
            succeeded.raise_for_status()
            detail = client.get(f"/api/v1/ingestions/{succeeded.json()['id']}")
            detail.raise_for_status()
            plain = client.post(
                "/api/v1/ingestions",
                json={
                    "source_type": "upload",
                    "uri": "upload://runtime.txt",
                    "filename": "runtime.txt",
                    "media_type": "text/plain",
                    "content_base64": base64.b64encode(b"Runtime plain notes").decode(),
                },
            )
            plain.raise_for_status()
            failed = client.post(
                "/api/v1/ingestions",
                json={
                    "source_type": "upload",
                    "uri": "upload://broken.pdf",
                    "filename": "broken.pdf",
                    "media_type": "application/pdf",
                    "content_base64": base64.b64encode(b"not a pdf").decode(),
                },
            )
            assert failed.status_code == 422
            failed_run_id = failed.json()["detail"]["run_id"]
            failed_detail = client.get(f"/api/v1/ingestions/{failed_run_id}")
            failed_detail.raise_for_status()
            assert failed_detail.json()["status"] == "failed"
            print(
                json.dumps(
                    {
                        "success_status": succeeded.json()["status"],
                        "candidate_type": detail.json()["candidates"][0]["candidate_type"],
                        "candidate_customer": detail.json()["candidates"][0]["data"]["customer"],
                        "plain_status": plain.json()["status"],
                        "failed_status": failed_detail.json()["status"],
                        "failed_code": failed_detail.json()["error_code"],
                    }
                )
            )
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            for table in (
                "extraction_candidates",
                "ingestion_runs",
                "sources",
                "memberships",
            ):
                cursor.execute(
                    f"DELETE FROM {table} WHERE organization_id=%s",  # noqa: S608
                    (organization_id,),
                )
            cursor.execute("DELETE FROM users WHERE organization_id=%s", (organization_id,))
            cursor.execute(
                "DELETE FROM workspaces WHERE organization_id=%s", (organization_id,)
            )
            cursor.execute("DELETE FROM organizations WHERE id=%s", (organization_id,))
        connection.commit()
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"phase4 runtime smoke failed: {error}", file=sys.stderr)
        raise
