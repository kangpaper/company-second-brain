import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

async function fillConnectionForm(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("API token"), "workspace-api-token");
  await user.type(
    screen.getByLabelText("Organization ID"),
    "11111111-1111-4111-8111-111111111111",
  );
  await user.type(
    screen.getByLabelText("Workspace ID"),
    "22222222-2222-4222-8222-222222222222",
  );
  await user.type(screen.getByLabelText("MCP endpoint"), "https://atlas.example/mcp");
  await user.type(screen.getByLabelText("MCP access token"), "connector-secret");
}

function pendingMcpIntake(filename: string) {
  return {
    id: "77777777-7777-4777-8777-777777777777",
    source_id: "44444444-4444-4444-8444-444444444444",
    source_asset_id: "88888888-8888-4888-8888-888888888888",
    status: "succeeded",
    filename,
    media_type: "text/markdown",
    byte_size: 42,
    candidate_count: 1,
    classification: {
      document_type: "customer_brief",
      confidence: 0.94,
      method: "deterministic-rules.v1",
      reason: "Matched customer brief indicators",
    },
    normalized_markdown: `# ${filename}`,
    review_status: "pending",
    reviewed_by: null,
    reviewed_at: null,
    review_reason: null,
    document_id: null,
    document_version_id: null,
  };
}

describe("operator workbench shell", () => {
  it("shows session scope and generic MCP workflow without making an eager request", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    render(<App />);

    expect(
      screen.getByText("Knowledge operations · Phase 16"),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Turn source material into trusted memory" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Document intake" })).toBeVisible();
    expect(screen.getByLabelText("API token")).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("Organization ID")).toBeVisible();
    expect(screen.getByLabelText("Workspace ID")).toBeVisible();
    expect(screen.getByLabelText("MCP endpoint")).toBeVisible();
    expect(screen.getByLabelText("MCP access token")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Test connection" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Load resources" })).toBeEnabled();
    expect(screen.getByText("Review queue not loaded")).toBeVisible();
    expect(screen.queryByText("No documents waiting")).not.toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("distinguishes an unrequested and loading review queue from a confirmed empty queue", async () => {
    const user = userEvent.setup();
    let resolveFetch!: (response: Response) => void;
    const pendingResponse = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockReturnValue(pendingResponse);
    render(<App />);
    await fillConnectionForm(user);

    await user.click(screen.getByRole("button", { name: "Refresh review queue" }));

    expect(screen.getByText("Loading review queue…")).toBeVisible();
    expect(screen.queryByText("No documents waiting")).not.toBeInTheDocument();

    resolveFetch(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    expect(await screen.findByText("No documents waiting")).toBeVisible();
    expect(screen.queryByText("Loading review queue…")).not.toBeInTheDocument();
  });

  it("tests an MCP connection with scoped headers and keeps secrets out of storage", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          connected: true,
          server_info: { name: "Atlas MCP", version: "1.4.0" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<App />);

    await user.type(screen.getByLabelText("API token"), "workspace-api-token");
    await user.type(
      screen.getByLabelText("Organization ID"),
      "11111111-1111-4111-8111-111111111111",
    );
    await user.type(
      screen.getByLabelText("Workspace ID"),
      "22222222-2222-4222-8222-222222222222",
    );
    await user.type(screen.getByLabelText("MCP endpoint"), "https://atlas.example/mcp");
    await user.type(screen.getByLabelText("MCP access token"), "connector-secret");
    await user.click(screen.getByRole("button", { name: "Test connection" }));

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/integrations/mcp/test-connection",
      expect.objectContaining({
        method: "POST",
        headers: {
          Authorization: "Bearer workspace-api-token",
          "Content-Type": "application/json",
          "X-Organization-ID": "11111111-1111-4111-8111-111111111111",
          "X-Workspace-ID": "22222222-2222-4222-8222-222222222222",
        },
        body: JSON.stringify({
          endpoint_url: "https://atlas.example/mcp",
          access_token: "connector-secret",
        }),
      }),
    );
    expect(await screen.findByText(/Connected to Atlas MCP/)).toBeVisible();
    expect(window.localStorage).toHaveLength(0);
    expect(window.sessionStorage).toHaveLength(0);
  });

  it("lists bounded MCP resources and sends one to the pending review queue", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (input === "/api/v1/integrations/mcp/resources/list") {
        return new Response(
          JSON.stringify({
            resources: [
              {
                uri: "mcp://atlas/customer-brief",
                name: "Customer brief",
                description: "Account context and current commitments.",
                mimeType: "text/markdown",
                size: 1842,
              },
            ],
            next_cursor: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (input === "/api/v1/integrations/mcp/resources/intake") {
        return new Response(
          JSON.stringify({
            id: "77777777-7777-4777-8777-777777777777",
            source_id: "44444444-4444-4444-8444-444444444444",
            source_asset_id: "88888888-8888-4888-8888-888888888888",
            status: "succeeded",
            filename: "Customer brief.md",
            media_type: "text/markdown",
            byte_size: 1842,
            candidate_count: 1,
            classification: {
              document_type: "customer_brief",
              confidence: 0.94,
              method: "deterministic-rules.v1",
              reason: "Matched customer brief indicators",
            },
            normalized_markdown: "# Customer brief",
            review_status: "pending",
            reviewed_by: null,
            reviewed_at: null,
            review_reason: null,
            document_id: null,
            document_version_id: null,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    render(<App />);
    await fillConnectionForm(user);

    await user.click(screen.getByRole("button", { name: "Load resources" }));

    expect(await screen.findByRole("heading", { name: "Customer brief" })).toBeVisible();
    expect(screen.getByText("Account context and current commitments.")).toBeVisible();
    expect(screen.getByText("text/markdown · 1.8 KB")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Send Customer brief to review" }));

    expect(await screen.findByText("Customer brief.md")).toBeVisible();
    expect(screen.getByText("Pending operator review")).toBeVisible();
    expect(screen.queryByText(/Created canonical document/)).not.toBeInTheDocument();
    expect(fetchSpy).toHaveBeenLastCalledWith(
      "/api/v1/integrations/mcp/resources/intake",
      expect.objectContaining({
        body: JSON.stringify({
          endpoint_url: "https://atlas.example/mcp",
          access_token: "connector-secret",
          resource_uri: "mcp://atlas/customer-brief",
        }),
      }),
    );
  });

  it("searches canonical knowledge within the active tenant scope", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            document_id: "33333333-3333-4333-8333-333333333333",
            title: "Customer brief",
            snippet: "Atlas renewal is due in October.",
            score: 1,
          },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<App />);
    await user.type(screen.getByLabelText("API token"), "workspace-api-token");
    await user.type(
      screen.getByLabelText("Organization ID"),
      "11111111-1111-4111-8111-111111111111",
    );
    await user.type(
      screen.getByLabelText("Workspace ID"),
      "22222222-2222-4222-8222-222222222222",
    );
    await user.type(screen.getByLabelText("Search knowledge"), "Atlas renewal");

    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByRole("heading", { name: "Customer brief" })).toBeVisible();
    expect(screen.getByText("Atlas renewal is due in October.")).toBeVisible();
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/search?q=Atlas%20renewal",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({
          Authorization: "Bearer workspace-api-token",
          "X-Organization-ID": "11111111-1111-4111-8111-111111111111",
          "X-Workspace-ID": "22222222-2222-4222-8222-222222222222",
        }),
      }),
    );
  });

  it("loads the bounded sanitized MCP audit feed for the active tenant", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: "66666666-6666-4666-8666-666666666666",
            provider: "mcp",
            operation: "import_resource",
            tool_name: "resources/read",
            outcome: "succeeded",
            error_code: null,
            error_message: null,
            created_at: "2026-08-24T12:00:00Z",
          },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<App />);
    await user.type(screen.getByLabelText("API token"), "workspace-api-token");
    await user.type(
      screen.getByLabelText("Organization ID"),
      "11111111-1111-4111-8111-111111111111",
    );
    await user.type(
      screen.getByLabelText("Workspace ID"),
      "22222222-2222-4222-8222-222222222222",
    );

    await user.click(screen.getByRole("button", { name: "Refresh audit" }));

    expect(await screen.findByText("Import resource")).toBeVisible();
    expect(screen.getByText("Succeeded")).toBeVisible();
    expect(screen.getByText("resources/read")).toBeVisible();
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/integration-audits?provider=mcp&limit=20",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({
          Authorization: "Bearer workspace-api-token",
          "X-Organization-ID": "11111111-1111-4111-8111-111111111111",
          "X-Workspace-ID": "22222222-2222-4222-8222-222222222222",
        }),
      }),
    );
    expect(document.body).not.toHaveTextContent("secret-host.example");
  });

  it("clears completed connector-derived data when connector credentials change", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          resources: [{ uri: "mcp://atlas/current", name: "Current connector resource" }],
          next_cursor: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<App />);
    await fillConnectionForm(user);
    await user.click(screen.getByRole("button", { name: "Load resources" }));
    expect(await screen.findByText("Current connector resource")).toBeVisible();

    await user.clear(screen.getByLabelText("MCP endpoint"));
    await user.type(screen.getByLabelText("MCP endpoint"), "https://other.example/mcp");

    expect(screen.queryByText("Current connector resource")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "No resources loaded" })).toBeVisible();
  });

  it("invalidates scoped results and ignores a stale response after the scope changes", async () => {
    const user = userEvent.setup();
    let resolveResources!: (response: Response) => void;
    const pendingResources = new Promise<Response>((resolve) => {
      resolveResources = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockReturnValue(pendingResources);
    render(<App />);
    await fillConnectionForm(user);

    await user.click(screen.getByRole("button", { name: "Load resources" }));
    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();

    await user.clear(screen.getByLabelText("Workspace ID"));
    await user.type(
      screen.getByLabelText("Workspace ID"),
      "99999999-9999-4999-8999-999999999999",
    );
    expect(screen.getByRole("heading", { name: "No resources loaded" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Load resources" })).toBeEnabled();

    resolveResources(
      new Response(
        JSON.stringify({
          resources: [{ uri: "mcp://old/resource", name: "Old workspace resource" }],
          next_cursor: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await vi.waitFor(() => {
      expect(screen.queryByText("Old workspace resource")).not.toBeInTheDocument();
    });
  });

  it("accepts an unnamed bounded resource and renders a safe fallback", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          resources: [{ uri: "mcp://atlas/unnamed", mimeType: "text/plain" }],
          next_cursor: null,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<App />);
    await fillConnectionForm(user);

    await user.click(screen.getByRole("button", { name: "Load resources" }));

    expect(await screen.findByRole("heading", { name: "Unnamed MCP resource" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Send Unnamed MCP resource to review" }),
    ).toBeEnabled();
  });

  it("uploads a document into the review queue without overriding multipart boundaries", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "77777777-7777-4777-8777-777777777777",
          source_id: "44444444-4444-4444-8444-444444444444",
          source_asset_id: "88888888-8888-4888-8888-888888888888",
          status: "succeeded",
          filename: "renewal-meeting.txt",
          media_type: "text/plain",
          content_hash: "a".repeat(64),
          byte_size: 58,
          parser_metadata: { parser: "plain_text" },
          candidate_count: 1,
          classification: {
            document_type: "meeting_notes",
            confidence: 0.94,
            method: "deterministic-rules.v1",
            reason: "Matched meeting-note indicators",
          },
          normalized_markdown: "---\ntitle: renewal-meeting\n---\n# Renewal meeting",
          review_status: "pending",
          reviewed_by: null,
          reviewed_at: null,
          review_reason: null,
          document_id: null,
          document_version_id: null,
          error_code: null,
          error_message: null,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<App />);
    await user.type(screen.getByLabelText("API token"), "workspace-api-token");
    await user.type(
      screen.getByLabelText("Organization ID"),
      "11111111-1111-4111-8111-111111111111",
    );
    await user.type(
      screen.getByLabelText("Workspace ID"),
      "22222222-2222-4222-8222-222222222222",
    );
    const file = new File(["Meeting notes: Atlas renewal and action items"], "renewal-meeting.txt", {
      type: "text/plain",
    });

    await user.upload(screen.getByLabelText("Choose documents"), file);
    await user.click(screen.getByRole("button", { name: "Upload for review" }));

    expect(await screen.findByRole("heading", { name: "renewal-meeting.txt" })).toBeVisible();
    expect(screen.getByText("Meeting notes")).toBeVisible();
    expect(screen.getByText("94% confidence")).toBeVisible();
    expect(screen.getByText("Matched meeting-note indicators")).toBeVisible();
    expect(screen.getByText("# Renewal meeting")).toBeVisible();
    const request = fetchSpy.mock.calls[0];
    expect(request[0]).toBe("/api/v1/ingestions/upload");
    expect(request[1]).toEqual(
      expect.objectContaining({
        method: "POST",
        headers: {
          Authorization: "Bearer workspace-api-token",
          "X-Organization-ID": "11111111-1111-4111-8111-111111111111",
          "X-Workspace-ID": "22222222-2222-4222-8222-222222222222",
        },
        body: expect.any(FormData),
      }),
    );
  });

  it("rejects a pending document with an operator reason", async () => {
    const user = userEvent.setup();
    const pending = {
      id: "77777777-7777-4777-8777-777777777777",
      source_id: "44444444-4444-4444-8444-444444444444",
      source_asset_id: "88888888-8888-4888-8888-888888888888",
      status: "succeeded",
      filename: "renewal-meeting.txt",
      media_type: "text/plain",
      byte_size: 58,
      candidate_count: 1,
      classification: {
        document_type: "meeting_notes",
        confidence: 0.94,
        method: "deterministic-rules.v1",
        reason: "Matched meeting-note indicators",
      },
      normalized_markdown: "# Renewal meeting",
      review_status: "pending",
      reviewed_by: null,
      reviewed_at: null,
      review_reason: null,
      document_id: null,
      document_version_id: null,
    };
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (input === "/api/v1/ingestions/upload") {
        return new Response(JSON.stringify(pending), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (input === "/api/v1/ingestions/77777777-7777-4777-8777-777777777777/reject") {
        return new Response(
          JSON.stringify({
            ...pending,
            review_status: "rejected",
            reviewed_by: "99999999-9999-4999-8999-999999999999",
            reviewed_at: "2026-08-25T10:00:00Z",
            review_reason: "Duplicate source",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    render(<App />);
    await user.type(screen.getByLabelText("API token"), "workspace-api-token");
    await user.type(
      screen.getByLabelText("Organization ID"),
      "11111111-1111-4111-8111-111111111111",
    );
    await user.type(
      screen.getByLabelText("Workspace ID"),
      "22222222-2222-4222-8222-222222222222",
    );
    await user.upload(
      screen.getByLabelText("Choose documents"),
      new File(["Meeting notes"], "renewal-meeting.txt", { type: "text/plain" }),
    );
    await user.click(screen.getByRole("button", { name: "Upload for review" }));
    await user.type(
      await screen.findByLabelText("Rejection reason for renewal-meeting.txt"),
      "Duplicate source",
    );
    await user.click(screen.getByRole("button", { name: "Reject renewal-meeting.txt" }));

    expect(await screen.findByText("Rejected renewal-meeting.txt: Duplicate source")).toBeVisible();
    expect(fetchSpy).toHaveBeenLastCalledWith(
      "/api/v1/ingestions/77777777-7777-4777-8777-777777777777/reject",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ reason: "Duplicate source" }),
      }),
    );
  });

  it("loads the pending review queue and promotes a reviewed document", async () => {
    const user = userEvent.setup();
    const pending = {
      id: "77777777-7777-4777-8777-777777777777",
      source_id: "44444444-4444-4444-8444-444444444444",
      source_asset_id: "88888888-8888-4888-8888-888888888888",
      status: "succeeded",
      filename: "renewal-meeting.txt",
      media_type: "text/plain",
      content_hash: "a".repeat(64),
      byte_size: 58,
      parser_metadata: { parser: "plain_text" },
      candidate_count: 1,
      classification: {
        document_type: "meeting_notes",
        confidence: 0.94,
        method: "deterministic-rules.v1",
        reason: "Matched meeting-note indicators",
      },
      normalized_markdown: "---\ntitle: renewal-meeting\n---\n# Renewal meeting",
      review_status: "pending",
      reviewed_by: null,
      reviewed_at: null,
      review_reason: null,
      document_id: null,
      document_version_id: null,
      error_code: null,
      error_message: null,
    };
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (input === "/api/v1/ingestions?review_status=pending&limit=50") {
        return new Response(JSON.stringify([pending]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (input === "/api/v1/ingestions/77777777-7777-4777-8777-777777777777/promote") {
        return new Response(
          JSON.stringify({
            ...pending,
            review_status: "promoted",
            reviewed_by: "99999999-9999-4999-8999-999999999999",
            reviewed_at: "2026-08-25T10:00:00Z",
            document_id: "33333333-3333-4333-8333-333333333333",
            document_version_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    render(<App />);
    await user.type(screen.getByLabelText("API token"), "workspace-api-token");
    await user.type(
      screen.getByLabelText("Organization ID"),
      "11111111-1111-4111-8111-111111111111",
    );
    await user.type(
      screen.getByLabelText("Workspace ID"),
      "22222222-2222-4222-8222-222222222222",
    );

    await user.click(screen.getByRole("button", { name: "Refresh review queue" }));
    expect(await screen.findByRole("heading", { name: "renewal-meeting.txt" })).toBeVisible();
    await user.type(
      screen.getByLabelText("Canonical Markdown path for renewal-meeting.txt"),
      "customers/atlas/renewal-meeting.md",
    );
    await user.click(screen.getByRole("button", { name: "Promote renewal-meeting.txt" }));

    expect(
      await screen.findByText("Promoted renewal-meeting.txt to canonical knowledge"),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: "renewal-meeting.txt" })).not.toBeInTheDocument();
    expect(fetchSpy).toHaveBeenLastCalledWith(
      "/api/v1/ingestions/77777777-7777-4777-8777-777777777777/promote",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ path: "customers/atlas/renewal-meeting.md" }),
      }),
    );
  });

  it("rejects failed runs returned in the actionable review queue", async () => {
    const user = userEvent.setup();
    const failedPending = {
      id: "77777777-7777-4777-8777-777777777777",
      source_id: "44444444-4444-4444-8444-444444444444",
      source_asset_id: "88888888-8888-4888-8888-888888888888",
      status: "failed",
      filename: "broken.pdf",
      media_type: "application/pdf",
      content_hash: "a".repeat(64),
      byte_size: 9,
      parser_metadata: {},
      candidate_count: 0,
      classification: null,
      normalized_markdown: null,
      review_status: "pending",
      reviewed_by: null,
      reviewed_at: null,
      review_reason: null,
      document_id: null,
      document_version_id: null,
      error_code: "parse_error",
      error_message: "Invalid PDF",
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify([failedPending]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    render(<App />);
    await user.type(screen.getByLabelText("API token"), "workspace-api-token");
    await user.type(
      screen.getByLabelText("Organization ID"),
      "11111111-1111-4111-8111-111111111111",
    );
    await user.type(
      screen.getByLabelText("Workspace ID"),
      "22222222-2222-4222-8222-222222222222",
    );

    await user.click(screen.getByRole("button", { name: "Refresh review queue" }));

    expect(await screen.findByText("The server returned an invalid review queue")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "broken.pdf" })).not.toBeInTheDocument();
  });

  it("keeps the global intake lock across connector invalidation", async () => {
    const user = userEvent.setup();
    let resolveImport!: (response: Response) => void;
    const pendingImport = new Promise<Response>((resolve) => {
      resolveImport = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (input === "/api/v1/integrations/mcp/resources/list") {
        return new Response(
          JSON.stringify({
            resources: [
              { uri: "mcp://atlas/one", name: "Resource one" },
              { uri: "mcp://atlas/two", name: "Resource two" },
            ],
            next_cursor: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (input === "/api/v1/integrations/mcp/resources/intake") return pendingImport;
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    render(<App />);
    await fillConnectionForm(user);
    await user.click(screen.getByRole("button", { name: "Load resources" }));
    await user.click(
      await screen.findByRole("button", { name: "Send Resource one to review" }),
    );

    await user.clear(screen.getByLabelText("MCP endpoint"));
    await user.type(screen.getByLabelText("MCP endpoint"), "https://other.example/mcp");
    await user.click(screen.getByRole("button", { name: "Load resources" }));

    expect(
      await screen.findByRole("button", { name: "Send Resource two to review" }),
    ).toBeDisabled();

    resolveImport(
      new Response(
        JSON.stringify(pendingMcpIntake("Resource one.md")),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );
    await vi.waitFor(() => {
      expect(screen.getByRole("button", { name: "Send Resource two to review" })).toBeEnabled();
    });
    expect(screen.queryByText("Created canonical document Resource one")).not.toBeInTheDocument();
  });

  it("serializes intakes so another resource cannot start while one is pending", async () => {
    const user = userEvent.setup();
    let resolveImport!: (response: Response) => void;
    const pendingImport = new Promise<Response>((resolve) => {
      resolveImport = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      if (input === "/api/v1/integrations/mcp/resources/list") {
        return new Response(
          JSON.stringify({
            resources: [
              { uri: "mcp://atlas/one", name: "Resource one" },
              { uri: "mcp://atlas/two", name: "Resource two" },
            ],
            next_cursor: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (input === "/api/v1/integrations/mcp/resources/intake") return pendingImport;
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    render(<App />);
    await fillConnectionForm(user);
    await user.click(screen.getByRole("button", { name: "Load resources" }));

    const first = await screen.findByRole("button", { name: "Send Resource one to review" });
    const second = screen.getByRole("button", { name: "Send Resource two to review" });
    await user.click(first);

    expect(screen.getByRole("button", { name: "Sending Resource one to review" })).toBeDisabled();
    expect(second).toBeDisabled();

    resolveImport(
      new Response(
        JSON.stringify(pendingMcpIntake("Resource one.md")),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );
    expect(await screen.findByText("Resource one.md")).toBeVisible();
    expect(screen.getByText("Pending operator review")).toBeVisible();
    expect(screen.queryByText(/Created canonical document/)).not.toBeInTheDocument();
    expect(second).toBeEnabled();
  });
});
