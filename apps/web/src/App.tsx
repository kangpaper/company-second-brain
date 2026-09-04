import { useRef, useState } from "react";
import {
  intakeMcpResource,
  listIntegrationAudits,
  listMcpResources,
  listPendingIngestions,
  promoteIngestion,
  readableError,
  rejectIngestion,
  searchKnowledge,
  testMcpConnection,
  uploadIngestion,
  type ConnectorDraft,
  type ConnectionResult,
  type IngestionItem,
  type IntegrationAuditItem,
  type McpResource,
  type SearchResult,
  type SessionScope,
} from "./api";

const EMPTY_SCOPE: SessionScope = {
  apiToken: "",
  organizationId: "",
  workspaceId: "",
};

const EMPTY_CONNECTOR: ConnectorDraft = {
  endpointUrl: "",
  accessToken: "",
};

export function App() {
  const [scope, setScope] = useState(EMPTY_SCOPE);
  const [connector, setConnector] = useState(EMPTY_CONNECTOR);
  const [connection, setConnection] = useState<ConnectionResult | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [connectionPending, setConnectionPending] = useState(false);
  const [resources, setResources] = useState<McpResource[] | null>(null);
  const [resourcesPending, setResourcesPending] = useState(false);
  const [resourcesError, setResourcesError] = useState<string | null>(null);
  const [intakingUri, setIntakingUri] = useState<string | null>(null);
  const [intakes, setIntakes] = useState<Record<string, IngestionItem>>({});
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null);
  const [searchPending, setSearchPending] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [audits, setAudits] = useState<IntegrationAuditItem[] | null>(null);
  const [auditsPending, setAuditsPending] = useState(false);
  const [auditsError, setAuditsError] = useState<string | null>(null);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [reviewItems, setReviewItems] = useState<IngestionItem[] | null>(null);
  const [uploadPending, setUploadPending] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [reviewPending, setReviewPending] = useState(false);
  const [reviewActionId, setReviewActionId] = useState<string | null>(null);
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const activeImportGeneration = useRef<number | null>(null);

  function invalidateDerivedState() {
    requestGeneration.current += 1;
    setConnection(null);
    setConnectionError(null);
    setConnectionPending(false);
    setResources(null);
    setResourcesPending(false);
    setResourcesError(null);
    setIntakes({});
    setSearchResults(null);
    setSearchPending(false);
    setSearchError(null);
    setAudits(null);
    setAuditsPending(false);
    setAuditsError(null);
    setReviewItems(null);
    setUploadError(null);
    setUploadPending(false);
    setUploadProgress(0);
    setReviewPending(false);
    setReviewActionId(null);
    setReviewNotice(null);
  }

  function updateScope(field: keyof SessionScope, value: string) {
    invalidateDerivedState();
    setScope((current) => ({ ...current, [field]: value }));
  }

  function updateConnector(field: keyof ConnectorDraft, value: string) {
    invalidateDerivedState();
    setConnector((current) => ({ ...current, [field]: value }));
  }

  async function handleTestConnection() {
    const generation = requestGeneration.current;
    const requestScope = { ...scope };
    const requestConnector = { ...connector };
    setConnectionPending(true);
    setConnection(null);
    setConnectionError(null);
    try {
      const result = await testMcpConnection(requestScope, requestConnector);
      if (generation === requestGeneration.current) setConnection(result);
    } catch (error) {
      if (generation === requestGeneration.current) setConnectionError(readableError(error));
    } finally {
      if (generation === requestGeneration.current) setConnectionPending(false);
    }
  }

  async function handleLoadResources() {
    const generation = requestGeneration.current;
    const requestScope = { ...scope };
    const requestConnector = { ...connector };
    setResourcesPending(true);
    setResources(null);
    setIntakes({});
    setResourcesError(null);
    try {
      const result = await listMcpResources(requestScope, requestConnector);
      if (generation === requestGeneration.current) setResources(result);
    } catch (error) {
      if (generation === requestGeneration.current) {
        setResources(null);
        setResourcesError(readableError(error));
      }
    } finally {
      if (generation === requestGeneration.current) setResourcesPending(false);
    }
  }

  async function handleIntake(resource: McpResource) {
    if (activeImportGeneration.current !== null) return;
    const generation = requestGeneration.current;
    activeImportGeneration.current = generation;
    const requestScope = { ...scope };
    const requestConnector = { ...connector };
    setIntakingUri(resource.uri);
    setResourcesError(null);
    try {
      const result = await intakeMcpResource(requestScope, requestConnector, resource.uri);
      if (generation === requestGeneration.current) {
        setIntakes((current) => ({ ...current, [resource.uri]: result }));
        setReviewItems((current) => [
          result,
          ...(current ?? []).filter((item) => item.id !== result.id),
        ]);
        setReviewNotice(`${result.filename} is pending operator review`);
      }
    } catch (error) {
      if (generation === requestGeneration.current) setResourcesError(readableError(error));
    } finally {
      if (activeImportGeneration.current === generation) {
        activeImportGeneration.current = null;
        setIntakingUri(null);
      }
    }
  }

  async function handleSearch() {
    if (!searchQuery.trim()) return;
    const generation = requestGeneration.current;
    const requestScope = { ...scope };
    const requestQuery = searchQuery.trim();
    setSearchPending(true);
    setSearchError(null);
    try {
      const result = await searchKnowledge(requestScope, requestQuery);
      if (generation === requestGeneration.current) setSearchResults(result);
    } catch (error) {
      if (generation === requestGeneration.current) {
        setSearchResults(null);
        setSearchError(readableError(error));
      }
    } finally {
      if (generation === requestGeneration.current) setSearchPending(false);
    }
  }

  async function handleRefreshAudits() {
    const generation = requestGeneration.current;
    const requestScope = { ...scope };
    setAuditsPending(true);
    setAuditsError(null);
    try {
      const result = await listIntegrationAudits(requestScope);
      if (generation === requestGeneration.current) setAudits(result);
    } catch (error) {
      if (generation === requestGeneration.current) {
        setAudits(null);
        setAuditsError(readableError(error));
      }
    } finally {
      if (generation === requestGeneration.current) setAuditsPending(false);
    }
  }

  async function handleUpload() {
    if (selectedFiles.length === 0 || uploadPending) return;
    const generation = requestGeneration.current;
    const requestScope = { ...scope };
    const files = [...selectedFiles];
    setUploadPending(true);
    setUploadError(null);
    setUploadProgress(0);
    try {
      for (const [index, file] of files.entries()) {
        const result = await uploadIngestion(requestScope, file);
        if (generation !== requestGeneration.current) return;
        setReviewItems((current) => [
          result,
          ...(current ?? []).filter((item) => item.id !== result.id),
        ]);
        setUploadProgress(index + 1);
      }
      if (generation === requestGeneration.current) setSelectedFiles([]);
    } catch (error) {
      if (generation === requestGeneration.current) setUploadError(readableError(error));
    } finally {
      if (generation === requestGeneration.current) setUploadPending(false);
    }
  }

  async function handleRefreshReviewQueue() {
    const generation = requestGeneration.current;
    const requestScope = { ...scope };
    setReviewPending(true);
    setUploadError(null);
    setReviewNotice(null);
    try {
      const result = await listPendingIngestions(requestScope);
      if (generation === requestGeneration.current) setReviewItems(result);
    } catch (error) {
      if (generation === requestGeneration.current) setUploadError(readableError(error));
    } finally {
      if (generation === requestGeneration.current) setReviewPending(false);
    }
  }

  async function handlePromote(item: IngestionItem, path: string) {
    if (reviewActionId !== null) return;
    const generation = requestGeneration.current;
    const requestScope = { ...scope };
    setReviewActionId(item.id);
    setUploadError(null);
    setReviewNotice(null);
    try {
      const result = await promoteIngestion(requestScope, item.id, path);
      if (generation !== requestGeneration.current) return;
      setReviewItems((current) =>
        (current ?? []).filter((candidate) => candidate.id !== result.id),
      );
      setReviewNotice(`Promoted ${item.filename} to canonical knowledge`);
    } catch (error) {
      if (generation === requestGeneration.current) setUploadError(readableError(error));
    } finally {
      if (generation === requestGeneration.current) setReviewActionId(null);
    }
  }

  async function handleReject(item: IngestionItem, reason: string) {
    if (reviewActionId !== null) return;
    const generation = requestGeneration.current;
    const requestScope = { ...scope };
    setReviewActionId(item.id);
    setUploadError(null);
    setReviewNotice(null);
    try {
      const result = await rejectIngestion(requestScope, item.id, reason);
      if (generation !== requestGeneration.current) return;
      setReviewItems((current) =>
        (current ?? []).filter((candidate) => candidate.id !== result.id),
      );
      setReviewNotice(`Rejected ${item.filename}: ${result.reviewReason ?? reason}`);
    } catch (error) {
      if (generation === requestGeneration.current) setUploadError(readableError(error));
    } finally {
      if (generation === requestGeneration.current) setReviewActionId(null);
    }
  }

  const scopeReady = Boolean(
    scope.apiToken.trim() && scope.organizationId.trim() && scope.workspaceId.trim(),
  );

  return (
    <div className="operator-shell">
      <aside className="operator-rail" aria-label="Operator navigation">
        <a className="wordmark" href="#workbench" aria-label="Company Second Brain home">
          <span className="wordmark-mark" aria-hidden="true">CSB</span>
          <span>Company<br />Second Brain</span>
        </a>
        <nav className="rail-nav" aria-label="Workbench sections">
          <a className="rail-link" href="#workbench">
            Workbench
          </a>
          <a className="rail-link" href="#intake">Intake</a>
          <a className="rail-link" href="#review">Review</a>
          <a className="rail-link" href="#resources">Sources</a>
          <a className="rail-link" href="#knowledge">Documents</a>
          <a className="rail-link" href="#audit">Activity</a>
        </nav>
        <div className="rail-foot">
          <span className="rail-status-dot" aria-hidden="true" />
          <span>Read-only MCP lifecycle</span>
        </div>
      </aside>

      <main className="operator-main" id="workbench">
        <header className="page-head">
          <div>
            <p className="signal-line">Knowledge operations · Phase 16</p>
            <h1>Turn source material into trusted memory</h1>
          </div>
          <p className="page-intro">
            Upload business documents, inspect deterministic classification, and promote only
            reviewed Markdown into versioned canonical knowledge. MCP intake remains available below.
          </p>
        </header>

        <section className="workspace-grid" aria-label="Connection workspace">
          <form
            className="ledger-panel"
            autoComplete="off"
            onSubmit={(event) => event.preventDefault()}
          >
            <PanelHeading index="01" title="Workspace session">
              Credentials stay in memory. The workspace token uses request headers; MCP
              credentials use scoped HTTPS request bodies.
            </PanelHeading>
            <div className="field-stack">
              <Field
                id="api-token"
                label="API token"
                type="password"
                value={scope.apiToken}
                onChange={(apiToken) => updateScope("apiToken", apiToken)}
                autoComplete="off"
              />
              <Field
                id="organization-id"
                label="Organization ID"
                value={scope.organizationId}
                onChange={(organizationId) => updateScope("organizationId", organizationId)}
                placeholder="UUID"
              />
              <Field
                id="workspace-id"
                label="Workspace ID"
                value={scope.workspaceId}
                onChange={(workspaceId) => updateScope("workspaceId", workspaceId)}
                placeholder="UUID"
              />
            </div>
          </form>

          <form
            className="ledger-panel ledger-panel-primary"
            autoComplete="off"
            onSubmit={(event) => event.preventDefault()}
          >
            <PanelHeading index="02" title="MCP connection">
              Only server-allowlisted HTTPS endpoints ending in /mcp are accepted.
            </PanelHeading>
            <div className="field-stack">
              <Field
                id="mcp-endpoint"
                label="MCP endpoint"
                value={connector.endpointUrl}
                onChange={(endpointUrl) => updateConnector("endpointUrl", endpointUrl)}
                placeholder="https://approved.example/mcp"
              />
              <Field
                id="mcp-token"
                label="MCP access token"
                type="password"
                value={connector.accessToken}
                onChange={(accessToken) => updateConnector("accessToken", accessToken)}
                autoComplete="off"
              />
            </div>
            <div className="action-row">
              <button
                className="button button-primary"
                type="button"
                disabled={connectionPending}
                onClick={handleTestConnection}
              >
                {connectionPending ? "Testing…" : "Test connection"}
              </button>
              <button
                className="button button-secondary"
                type="button"
                disabled={resourcesPending}
                onClick={handleLoadResources}
              >
                {resourcesPending ? "Loading…" : "Load resources"}
              </button>
            </div>
            <div className="operation-status" aria-live="polite">
              {connection ? (
                <p className="status-message status-success">
                  Connected to {connection.serverName}
                  {connection.serverVersion ? ` · ${connection.serverVersion}` : ""}
                </p>
              ) : null}
              {connectionError ? (
                <p className="status-message status-error" role="alert">{connectionError}</p>
              ) : null}
            </div>
          </form>
        </section>

        <DocumentIntake
          files={selectedFiles}
          items={reviewItems}
          pending={uploadPending}
          progress={uploadProgress}
          error={uploadError}
          scopeReady={scopeReady}
          refreshPending={reviewPending}
          actionId={reviewActionId}
          notice={reviewNotice}
          onFilesChange={setSelectedFiles}
          onUpload={handleUpload}
          onRefresh={handleRefreshReviewQueue}
          onPromote={handlePromote}
          onReject={handleReject}
        />

        <ResourceRegister
          resources={resources}
          error={resourcesError}
          pending={resourcesPending}
          intakingUri={intakingUri}
          intakes={intakes}
          onIntake={handleIntake}
        />
        <KnowledgeSearch
          query={searchQuery}
          results={searchResults}
          error={searchError}
          pending={searchPending}
          onQueryChange={setSearchQuery}
          onSearch={handleSearch}
        />
        <AuditFeed
          audits={audits}
          error={auditsError}
          pending={auditsPending}
          onRefresh={handleRefreshAudits}
        />
      </main>
    </div>
  );
}

type PanelHeadingProps = {
  index: string;
  title: string;
  children: string;
};

function PanelHeading({ index, title, children }: PanelHeadingProps) {
  return (
    <div className="panel-heading">
      <span className="step-index">{index}</span>
      <div>
        <h2>{title}</h2>
        <p>{children}</p>
      </div>
    </div>
  );
}

type DocumentIntakeProps = {
  files: File[];
  items: IngestionItem[] | null;
  pending: boolean;
  progress: number;
  error: string | null;
  scopeReady: boolean;
  refreshPending: boolean;
  actionId: string | null;
  notice: string | null;
  onFilesChange: (files: File[]) => void;
  onUpload: () => void;
  onRefresh: () => void;
  onPromote: (item: IngestionItem, path: string) => void;
  onReject: (item: IngestionItem, reason: string) => void;
};

function DocumentIntake({
  files,
  items,
  pending,
  progress,
  error,
  scopeReady,
  refreshPending,
  actionId,
  notice,
  onFilesChange,
  onUpload,
  onRefresh,
  onPromote,
  onReject,
}: DocumentIntakeProps) {
  const [paths, setPaths] = useState<Record<string, string>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  return (
    <section className="intake-workspace" id="intake" aria-labelledby="intake-heading">
      <div className="intake-control">
        <header className="register-heading">
          <p className="signal-line">Originals preserved · 10 MiB per file</p>
          <h2 id="intake-heading">Document intake</h2>
          <p>
            Add PDF, DOCX, XLSX, CSV, Markdown, text, or HTML. Every file stays pending
            until a person reviews its classification and normalized Markdown.
          </p>
        </header>
        <label className="file-drop" htmlFor="document-files">
          <span className="file-drop-title">Choose documents</span>
          <span>or drop files here · multiple files supported</span>
          <input
            id="document-files"
            aria-label="Choose documents"
            type="file"
            multiple
            accept=".pdf,.docx,.xlsx,.csv,.md,.markdown,.txt,.html,.htm"
            disabled={pending}
            onChange={(event) => onFilesChange(Array.from(event.target.files ?? []))}
          />
        </label>
        <div className="selected-files" aria-live="polite">
          {files.length === 0 ? (
            <span>No files selected</span>
          ) : (
            <span>
              {files.length} selected · {files.map((file) => file.name).join(", ")}
            </span>
          )}
        </div>
        <button
          className="button button-primary"
          type="button"
          disabled={!scopeReady || files.length === 0 || pending}
          onClick={onUpload}
        >
          {pending ? `Uploading ${progress + 1} of ${files.length}…` : "Upload for review"}
        </button>
        {!scopeReady ? (
          <p className="intake-guidance">Enter the workspace session before uploading.</p>
        ) : null}
        {error ? <p className="register-error" role="alert">{error}</p> : null}
      </div>

      <div className="review-ledger" id="review" aria-labelledby="review-heading">
        <header className="review-heading">
          <div>
            <p className="signal-line">Human decision required</p>
            <h2 id="review-heading">Review queue</h2>
          </div>
          <div className="review-heading-actions">
            <span
              className="queue-count"
              aria-label={
                refreshPending
                  ? "Review queue loading"
                  : items === null
                    ? "Review queue not loaded"
                    : `${items.length} pending documents`
              }
            >
              {refreshPending
                ? "…"
                : items === null
                  ? "--"
                  : items.length.toString().padStart(2, "0")}
            </span>
            <button
              className="button button-secondary"
              type="button"
              disabled={!scopeReady || refreshPending || actionId !== null}
              onClick={onRefresh}
            >
              {refreshPending ? "Refreshing…" : "Refresh review queue"}
            </button>
          </div>
        </header>
        {notice ? <p className="status-message status-success" role="status">{notice}</p> : null}
        {refreshPending ? (
          <div className="review-empty" role="status">
            <strong>Loading review queue…</strong>
            <span>Checking the active workspace for pending documents.</span>
          </div>
        ) : items === null ? (
          <div className="review-empty">
            <strong>Review queue not loaded</strong>
            <span>Enter the workspace session, then refresh the queue.</span>
          </div>
        ) : items.length === 0 ? (
          <div className="review-empty">
            <strong>No documents waiting</strong>
            <span>Select source files to begin a reviewable intake.</span>
          </div>
        ) : (
          <ol className="review-list">
            {items.map((item) => {
              const classification = item.classification;
              const confidence = classification
                ? `${Math.round(classification.confidence * 100)}% confidence`
                : "Classification unavailable";
              const path = paths[item.id] ?? "";
              const reason = reasons[item.id] ?? "";
              return (
                <li className="review-item" key={item.id}>
                  <div className="review-summary">
                    <div>
                      <p className="review-type">
                        {classification
                          ? formatDocumentType(classification.documentType)
                          : "Unclassified"}
                      </p>
                      <h3>{item.filename}</h3>
                    </div>
                    <span className="confidence-tag">{confidence}</span>
                  </div>
                  {classification ? (
                    <p className="classification-reason">{classification.reason}</p>
                  ) : null}
                  {classification &&
                  (classification.confidence < 0.7 ||
                    classification.documentType === "unclassified") ? (
                    <p className="review-warning">Confirm the document type before promotion.</p>
                  ) : null}
                  {item.normalizedMarkdown ? (
                    <details className="markdown-preview" open>
                      <summary>Normalized Markdown preview</summary>
                      <pre>
                        <code>
                          {item.normalizedMarkdown.split("\n").map((line, index) => (
                            <span key={`${item.id}-${index}`}>{line}{"\n"}</span>
                          ))}
                        </code>
                      </pre>
                    </details>
                  ) : null}
                  <div className="review-decision">
                    <div className="field">
                      <label htmlFor={`promotion-path-${item.id}`}>
                        Canonical Markdown path for {item.filename}
                      </label>
                      <input
                        id={`promotion-path-${item.id}`}
                        type="text"
                        maxLength={2048}
                        placeholder="customers/example/source-document.md"
                        value={path}
                        disabled={actionId !== null}
                        onChange={(event) =>
                          setPaths((current) => ({ ...current, [item.id]: event.target.value }))
                        }
                      />
                    </div>
                    <button
                      className="button button-primary"
                      type="button"
                      aria-label={`Promote ${item.filename}`}
                      disabled={!path.trim().endsWith(".md") || actionId !== null}
                      onClick={() => onPromote(item, path.trim())}
                    >
                      {actionId === item.id ? "Promoting…" : "Promote"}
                    </button>
                  </div>
                  <div className="review-decision rejection-decision">
                    <div className="field">
                      <label htmlFor={`rejection-reason-${item.id}`}>
                        Rejection reason for {item.filename}
                      </label>
                      <input
                        id={`rejection-reason-${item.id}`}
                        type="text"
                        minLength={3}
                        maxLength={2000}
                        placeholder="Duplicate, wrong source, or parsing issue"
                        value={reason}
                        disabled={actionId !== null}
                        onChange={(event) =>
                          setReasons((current) => ({ ...current, [item.id]: event.target.value }))
                        }
                      />
                    </div>
                    <button
                      className="button button-danger"
                      type="button"
                      aria-label={`Reject ${item.filename}`}
                      disabled={reason.trim().length < 3 || actionId !== null}
                      onClick={() => onReject(item, reason.trim())}
                    >
                      {actionId === item.id ? "Reviewing…" : "Reject"}
                    </button>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </section>
  );
}

function formatDocumentType(value: string): string {
  const words = value.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

type ResourceRegisterProps = {
  resources: McpResource[] | null;
  error: string | null;
  pending: boolean;
  intakingUri: string | null;
  intakes: Record<string, IngestionItem>;
  onIntake: (resource: McpResource) => void;
};

function ResourceRegister({
  resources,
  error,
  pending,
  intakingUri,
  intakes,
  onIntake,
}: ResourceRegisterProps) {
  return (
    <section className="resource-register" id="resources" aria-labelledby="resource-heading">
      <header className="register-heading">
        <h2 id="resource-heading">
          {resources === null
            ? "No resources loaded"
            : `${resources.length} resource${resources.length === 1 ? "" : "s"} available`}
        </h2>
        <p>
          {resources === null
            ? "Enter workspace and MCP credentials, then load the bounded resource list."
            : "Only projected public descriptors are shown. Intake remains read-only at the provider and requires operator review."}
        </p>
      </header>
      {pending ? <div className="resource-skeleton" aria-label="Loading resources" /> : null}
      {error ? <p className="register-error" role="alert">{error}</p> : null}
      {resources?.length === 0 ? (
        <p className="register-empty">This server did not expose any readable resources.</p>
      ) : null}
      {resources && resources.length > 0 ? (
        <ul className="resource-list">
          {resources.map((resource) => {
            const result = intakes[resource.uri];
            const resourceName = resource.name ?? "Unnamed MCP resource";
            const isIntaking = intakingUri === resource.uri;
            return (
              <li className="resource-item" key={resource.uri}>
                <div className="resource-copy">
                  <h3>{resourceName}</h3>
                  <p>{resource.description ?? "No public description supplied."}</p>
                  <p className="resource-meta">
                    {resource.mimeType ?? "Unknown media type"}
                    {resource.size === null ? "" : ` · ${formatBytes(resource.size)}`}
                  </p>
                </div>
                <div className="resource-action">
                  <button
                    className="button button-secondary"
                    type="button"
                    disabled={intakingUri !== null}
                    onClick={() => onIntake(resource)}
                    aria-label={`${isIntaking ? "Sending" : "Send"} ${resourceName} to review`}
                  >
                    {isIntaking ? "Sending…" : result ? "Send again" : "Send to review"}
                  </button>
                  {result ? (
                    <div className="import-result" aria-live="polite">
                      <strong>Pending operator review</strong>
                      <span>Ingestion {result.id}</span>
                      <span>No canonical document is created until promotion.</span>
                    </div>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}

type KnowledgeSearchProps = {
  query: string;
  results: SearchResult[] | null;
  error: string | null;
  pending: boolean;
  onQueryChange: (value: string) => void;
  onSearch: () => void;
};

function KnowledgeSearch({
  query,
  results,
  error,
  pending,
  onQueryChange,
  onSearch,
}: KnowledgeSearchProps) {
  return (
    <section className="knowledge-search" id="knowledge" aria-labelledby="knowledge-heading">
      <header className="register-heading">
        <h2 id="knowledge-heading">Search workspace memory</h2>
        <p>Query the latest immutable document versions inside the active tenant scope.</p>
      </header>
      <form
        className="search-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSearch();
        }}
      >
        <div className="field search-field">
          <label htmlFor="knowledge-query">Search knowledge</label>
          <input
            id="knowledge-query"
            type="search"
            value={query}
            maxLength={500}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="Customer, commitment, decision…"
          />
        </div>
        <button className="button button-primary" type="submit" disabled={pending || !query.trim()}>
          {pending ? "Searching…" : "Search"}
        </button>
      </form>
      {error ? <p className="register-error" role="alert">{error}</p> : null}
      {results?.length === 0 ? (
        <p className="register-empty">No canonical documents matched this query.</p>
      ) : null}
      {results && results.length > 0 ? (
        <ol className="search-results">
          {results.map((result) => (
            <li key={result.documentId}>
              <h3>{result.title}</h3>
              <p>{result.snippet}</p>
              <span>Document {result.documentId}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

type AuditFeedProps = {
  audits: IntegrationAuditItem[] | null;
  error: string | null;
  pending: boolean;
  onRefresh: () => void;
};

function AuditFeed({ audits, error, pending, onRefresh }: AuditFeedProps) {
  return (
    <section className="audit-feed" id="audit" aria-labelledby="audit-heading">
      <header className="register-heading audit-heading">
        <div>
          <h2 id="audit-heading">Recent MCP activity</h2>
          <p>Newest-first, tenant-scoped outcomes without endpoint or resource metadata.</p>
        </div>
        <button
          className="button button-secondary"
          type="button"
          disabled={pending}
          onClick={onRefresh}
        >
          {pending ? "Refreshing…" : "Refresh audit"}
        </button>
      </header>
      {error ? <p className="register-error" role="alert">{error}</p> : null}
      {audits === null && !pending && !error ? (
        <p className="register-empty">Refresh after connecting or importing to inspect recent activity.</p>
      ) : null}
      {audits?.length === 0 ? (
        <p className="register-empty">No MCP activity is recorded in this workspace.</p>
      ) : null}
      {audits && audits.length > 0 ? (
        <ol className="audit-list">
          {audits.map((audit) => (
            <li className="audit-item" key={audit.id}>
              <div>
                <h3>{formatOperation(audit.operation)}</h3>
                <p className="audit-meta">
                  {audit.toolName ? <span>{audit.toolName}</span> : <span>Provider operation</span>}
                  <time dateTime={audit.createdAt}>{formatAuditTime(audit.createdAt)}</time>
                </p>
                {audit.errorMessage ? (
                  <p className="audit-error">
                    {audit.errorCode ? `${audit.errorCode}: ` : ""}{audit.errorMessage}
                  </p>
                ) : null}
              </div>
              <span className={`audit-outcome audit-outcome-${audit.outcome}`}>
                {formatOperation(audit.outcome)}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

function formatOperation(value: string): string {
  const words = value.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function formatAuditTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}

type FieldProps = {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "password";
  placeholder?: string;
  autoComplete?: string;
};

function Field({
  id,
  label,
  value,
  onChange,
  type = "text",
  placeholder,
  autoComplete,
}: FieldProps) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type={type}
        value={value}
        placeholder={placeholder}
        autoComplete={autoComplete}
        onChange={(event) => onChange(event.target.value)}
      />
      <span className="field-helper" aria-hidden="true">&nbsp;</span>
    </div>
  );
}
