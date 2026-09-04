export type SessionScope = {
  apiToken: string;
  organizationId: string;
  workspaceId: string;
};

export type ConnectorDraft = {
  endpointUrl: string;
  accessToken: string;
};

export type ConnectionResult = {
  connected: true;
  serverName: string;
  serverVersion: string | null;
};

export type McpResource = {
  uri: string;
  name: string | null;
  description: string | null;
  mimeType: string | null;
  size: number | null;
};

export type SearchResult = {
  documentId: string;
  title: string;
  snippet: string;
  score: number;
};

export type IntegrationAuditItem = {
  id: string;
  provider: string;
  operation: string;
  toolName: string | null;
  outcome: "succeeded" | "failed" | "denied";
  errorCode: string | null;
  errorMessage: string | null;
  createdAt: string;
};

export type DocumentClassification = {
  documentType: string;
  confidence: number;
  method: string;
  reason: string;
};

export type IngestionItem = {
  id: string;
  sourceId: string;
  sourceAssetId: string | null;
  status: "succeeded" | "failed";
  filename: string;
  mediaType: string;
  byteSize: number;
  candidateCount: number;
  classification: DocumentClassification | null;
  normalizedMarkdown: string | null;
  reviewStatus: "pending" | "promoted" | "rejected";
  reviewedBy: string | null;
  reviewedAt: string | null;
  reviewReason: string | null;
  documentId: string | null;
  documentVersionId: string | null;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function scopedHeaders(scope: SessionScope, includeJsonContentType = true): HeadersInit {
  return {
    Authorization: "Bearer " + scope.apiToken,
    ...(includeJsonContentType ? { "Content-Type": "application/json" } : {}),
    "X-Organization-ID": scope.organizationId,
    "X-Workspace-ID": scope.workspaceId,
  };
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function safeApiMessage(status: number, payload: unknown): string {
  if (
    payload !== null &&
    typeof payload === "object" &&
    "detail" in payload &&
    typeof payload.detail === "string" &&
    payload.detail.length <= 200
  ) {
    return payload.detail;
  }
  if (status === 401) return "Authentication required";
  if (status === 403) return "Workspace access denied";
  if (status === 422) return "Check the endpoint and required fields";
  return "The operation could not be completed";
}

async function postJson(
  path: string,
  scope: SessionScope,
  connector: ConnectorDraft,
  extra: Record<string, string> = {},
): Promise<unknown> {
  const response = await fetch(path, {
    method: "POST",
    headers: scopedHeaders(scope),
    body: JSON.stringify({
      endpoint_url: connector.endpointUrl,
      access_token: connector.accessToken,
      ...extra,
    }),
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new ApiError(safeApiMessage(response.status, payload), response.status);
  }
  return payload;
}

function optionalString(
  record: Record<string, unknown>,
  key: string,
  maxLength: number,
): string | null {
  const value = record[key];
  return typeof value === "string" && value.length <= maxLength ? value : null;
}

export async function testMcpConnection(
  scope: SessionScope,
  connector: ConnectorDraft,
): Promise<ConnectionResult> {
  const payload = await postJson(
    "/api/v1/integrations/mcp/test-connection",
    scope,
    connector,
  );
  if (payload === null || typeof payload !== "object") {
    throw new ApiError("The server returned an invalid connection response", 502);
  }
  const record = payload as Record<string, unknown>;
  if (record.connected !== true) {
    throw new ApiError("The server returned an invalid connection response", 502);
  }
  const serverInfo =
    record.server_info !== null && typeof record.server_info === "object"
      ? record.server_info
      : {};
  const serverName =
    "name" in serverInfo && typeof serverInfo.name === "string" && serverInfo.name.length <= 120
      ? serverInfo.name
      : "MCP server";
  const serverVersion =
    "version" in serverInfo &&
    typeof serverInfo.version === "string" &&
    serverInfo.version.length <= 80
      ? serverInfo.version
      : null;
  return { connected: true, serverName, serverVersion };
}

function parseResource(value: unknown): McpResource {
  if (value === null || typeof value !== "object") {
    throw new ApiError("The server returned an invalid resource list", 502);
  }
  const record = value as Record<string, unknown>;
  const uri = optionalString(record, "uri", 2048);
  const name = optionalString(record, "name", 500);
  const size = record.size;
  if (
    uri === null ||
    uri.length === 0 ||
    (record.name !== undefined &&
      record.name !== null &&
      (name === null || name.length === 0)) ||
    (size !== undefined &&
      size !== null &&
      (typeof size !== "number" || !Number.isSafeInteger(size) || size < 0))
  ) {
    throw new ApiError("The server returned an invalid resource list", 502);
  }
  return {
    uri,
    name,
    description: optionalString(record, "description", 2000),
    mimeType: optionalString(record, "mimeType", 200),
    size: typeof size === "number" ? size : null,
  };
}

export async function listMcpResources(
  scope: SessionScope,
  connector: ConnectorDraft,
): Promise<McpResource[]> {
  const payload = await postJson(
    "/api/v1/integrations/mcp/resources/list",
    scope,
    connector,
  );
  if (
    payload === null ||
    typeof payload !== "object" ||
    !("resources" in payload) ||
    !Array.isArray(payload.resources) ||
    payload.resources.length > 200
  ) {
    throw new ApiError("The server returned an invalid resource list", 502);
  }
  return payload.resources.map(parseResource);
}

export async function intakeMcpResource(
  scope: SessionScope,
  connector: ConnectorDraft,
  resourceUri: string,
): Promise<IngestionItem> {
  const response = await fetch("/api/v1/integrations/mcp/resources/intake", {
    method: "POST",
    headers: scopedHeaders(scope),
    body: JSON.stringify({
      endpoint_url: connector.endpointUrl,
      access_token: connector.accessToken,
      resource_uri: resourceUri,
    }),
  });
  return parseIngestionResponse(response);
}

export async function searchKnowledge(
  scope: SessionScope,
  query: string,
): Promise<SearchResult[]> {
  const response = await fetch(`/api/v1/search?q=${encodeURIComponent(query)}`, {
    method: "GET",
    headers: scopedHeaders(scope),
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new ApiError(safeApiMessage(response.status, payload), response.status);
  }
  if (!Array.isArray(payload) || payload.length > 100) {
    throw new ApiError("The server returned invalid search results", 502);
  }
  return payload.map((value) => {
    if (value === null || typeof value !== "object") {
      throw new ApiError("The server returned invalid search results", 502);
    }
    const record = value as Record<string, unknown>;
    const documentId = optionalString(record, "document_id", 64);
    const title = optionalString(record, "title", 500);
    const snippet = optionalString(record, "snippet", 1000);
    if (
      documentId === null ||
      title === null ||
      snippet === null ||
      typeof record.score !== "number" ||
      !Number.isFinite(record.score)
    ) {
      throw new ApiError("The server returned invalid search results", 502);
    }
    return { documentId, title, snippet, score: record.score };
  });
}

export async function listIntegrationAudits(
  scope: SessionScope,
): Promise<IntegrationAuditItem[]> {
  const response = await fetch("/api/v1/integration-audits?provider=mcp&limit=20", {
    method: "GET",
    headers: scopedHeaders(scope),
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new ApiError(safeApiMessage(response.status, payload), response.status);
  }
  if (!Array.isArray(payload) || payload.length > 20) {
    throw new ApiError("The server returned an invalid audit feed", 502);
  }
  return payload.map((value) => {
    if (value === null || typeof value !== "object") {
      throw new ApiError("The server returned an invalid audit feed", 502);
    }
    const record = value as Record<string, unknown>;
    const id = optionalString(record, "id", 64);
    const provider = optionalString(record, "provider", 50);
    const operation = optionalString(record, "operation", 100);
    const toolName = optionalString(record, "tool_name", 100);
    const errorCode = optionalString(record, "error_code", 100);
    const errorMessage = optionalString(record, "error_message", 500);
    const createdAt = optionalString(record, "created_at", 64);
    const outcome = record.outcome;
    if (
      id === null ||
      provider === null ||
      operation === null ||
      createdAt === null ||
      !Number.isFinite(Date.parse(createdAt)) ||
      (outcome !== "succeeded" && outcome !== "failed" && outcome !== "denied")
    ) {
      throw new ApiError("The server returned an invalid audit feed", 502);
    }
    return {
      id,
      provider,
      operation,
      toolName,
      outcome,
      errorCode,
      errorMessage,
      createdAt,
    };
  });
}

function parseIngestion(value: unknown): IngestionItem {
  if (value === null || typeof value !== "object") {
    throw new ApiError("The server returned an invalid document intake response", 502);
  }
  const record = value as Record<string, unknown>;
  const id = optionalString(record, "id", 64);
  const sourceId = optionalString(record, "source_id", 64);
  const sourceAssetId = optionalString(record, "source_asset_id", 64);
  const filename = optionalString(record, "filename", 500);
  const mediaType = optionalString(record, "media_type", 255);
  const normalizedMarkdown = optionalString(record, "normalized_markdown", 2_000_000);
  const reviewedBy = optionalString(record, "reviewed_by", 64);
  const reviewedAt = optionalString(record, "reviewed_at", 64);
  const reviewReason = optionalString(record, "review_reason", 2000);
  const documentId = optionalString(record, "document_id", 64);
  const documentVersionId = optionalString(record, "document_version_id", 64);
  const byteSize = record.byte_size;
  const candidateCount = record.candidate_count;
  const statusValue = record.status;
  const reviewStatus = record.review_status;
  if (
    id === null ||
    sourceId === null ||
    filename === null ||
    mediaType === null ||
    (statusValue !== "succeeded" && statusValue !== "failed") ||
    !Number.isSafeInteger(byteSize) ||
    typeof byteSize !== "number" ||
    byteSize < 0 ||
    !Number.isSafeInteger(candidateCount) ||
    typeof candidateCount !== "number" ||
    candidateCount < 0 ||
    (reviewStatus !== "pending" &&
      reviewStatus !== "promoted" &&
      reviewStatus !== "rejected") ||
    (record.reviewed_at !== null &&
      record.reviewed_at !== undefined &&
      (reviewedAt === null || !Number.isFinite(Date.parse(reviewedAt))))
  ) {
    throw new ApiError("The server returned an invalid document intake response", 502);
  }

  let classification: DocumentClassification | null = null;
  if (record.classification !== null && record.classification !== undefined) {
    if (typeof record.classification !== "object") {
      throw new ApiError("The server returned an invalid document intake response", 502);
    }
    const raw = record.classification as Record<string, unknown>;
    const documentType = optionalString(raw, "document_type", 100);
    const method = optionalString(raw, "method", 100);
    const reason = optionalString(raw, "reason", 2000);
    const confidence = raw.confidence;
    if (
      documentType === null ||
      method === null ||
      reason === null ||
      typeof confidence !== "number" ||
      !Number.isFinite(confidence) ||
      confidence < 0 ||
      confidence > 1
    ) {
      throw new ApiError("The server returned an invalid document intake response", 502);
    }
    classification = { documentType, confidence, method, reason };
  }

  return {
    id,
    sourceId,
    sourceAssetId,
    status: statusValue,
    filename,
    mediaType,
    byteSize,
    candidateCount,
    classification,
    normalizedMarkdown,
    reviewStatus,
    reviewedBy,
    reviewedAt,
    reviewReason,
    documentId,
    documentVersionId,
  };
}

async function parseIngestionResponse(response: Response): Promise<IngestionItem> {
  const payload = await readJson(response);
  if (!response.ok) {
    throw new ApiError(safeApiMessage(response.status, payload), response.status);
  }
  return parseIngestion(payload);
}

export async function uploadIngestion(
  scope: SessionScope,
  file: File,
): Promise<IngestionItem> {
  const form = new FormData();
  form.append("file", file, file.name);
  const response = await fetch("/api/v1/ingestions/upload", {
    method: "POST",
    headers: scopedHeaders(scope, false),
    body: form,
  });
  return parseIngestionResponse(response);
}

export async function listPendingIngestions(
  scope: SessionScope,
): Promise<IngestionItem[]> {
  const response = await fetch("/api/v1/ingestions?review_status=pending&limit=50", {
    method: "GET",
    headers: scopedHeaders(scope),
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw new ApiError(safeApiMessage(response.status, payload), response.status);
  }
  if (!Array.isArray(payload) || payload.length > 50) {
    throw new ApiError("The server returned an invalid review queue", 502);
  }
  const items = payload.map(parseIngestion);
  if (
    items.some(
      (item) =>
        item.status !== "succeeded" ||
        item.reviewStatus !== "pending" ||
        item.sourceAssetId === null ||
        item.classification === null ||
        item.normalizedMarkdown === null ||
        item.reviewedBy !== null ||
        item.reviewedAt !== null ||
        item.documentId !== null ||
        item.documentVersionId !== null,
    )
  ) {
    throw new ApiError("The server returned an invalid review queue", 502);
  }
  return items;
}

export async function promoteIngestion(
  scope: SessionScope,
  ingestionId: string,
  path: string,
): Promise<IngestionItem> {
  const response = await fetch(`/api/v1/ingestions/${encodeURIComponent(ingestionId)}/promote`, {
    method: "POST",
    headers: scopedHeaders(scope),
    body: JSON.stringify({ path }),
  });
  return parseIngestionResponse(response);
}

export async function rejectIngestion(
  scope: SessionScope,
  ingestionId: string,
  reason: string,
): Promise<IngestionItem> {
  const response = await fetch(`/api/v1/ingestions/${encodeURIComponent(ingestionId)}/reject`, {
    method: "POST",
    headers: scopedHeaders(scope),
    body: JSON.stringify({ reason }),
  });
  return parseIngestionResponse(response);
}

export function readableError(error: unknown): string {
  return error instanceof ApiError ? error.message : "The operation could not be completed";
}
