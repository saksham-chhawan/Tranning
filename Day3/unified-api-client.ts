// unified-api-client.ts

type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

type ApiKind = "rest" | "graphql";

type Primitive = string | number | boolean | null | undefined;
type QueryValue = Primitive | Primitive[];

type HeadersMap = Record<string, string>;
type QueryParams = Record<string, QueryValue>;

interface CacheOptions {
  enabled?: boolean;
  ttlMs?: number;
  key?: string;
}

interface BaseRequestOptions {
  kind: ApiKind;
  headers?: HeadersMap;
  timeoutMs?: number;
  cache?: CacheOptions;
  signal?: AbortSignal;
}

interface RestRequestOptions extends BaseRequestOptions {
  kind: "rest";
  url: string;
  method?: HttpMethod;
  query?: QueryParams;
  body?: unknown;
}

interface GraphQLRequestOptions extends BaseRequestOptions {
  kind: "graphql";
  url: string;
  query: string;
  variables?: Record<string, unknown>;
  operationName?: string;
}

type UnifiedRequestOptions = RestRequestOptions | GraphQLRequestOptions;

interface UnifiedSuccess<T> {
  ok: true;
  status: number;
  data: T;
  headers: Headers;
  cached: boolean;
}

interface UnifiedFailure {
  ok: false;
  error: UnifiedApiError;
  status?: number;
  headers?: Headers;
  cached: boolean;
}

type UnifiedResponse<T> = UnifiedSuccess<T> | UnifiedFailure;

type ErrorCode =
  | "TIMEOUT"
  | "NETWORK_ERROR"
  | "HTTP_ERROR"
  | "GRAPHQL_ERROR"
  | "PARSE_ERROR"
  | "UNKNOWN_ERROR";

interface GraphQLErrorItem {
  message: string;
  path?: Array<string | number>;
  extensions?: Record<string, unknown>;
}

class UnifiedApiError extends Error {
  public readonly code: ErrorCode;
  public readonly status?: number;
  public readonly details?: unknown;
  public readonly cause?: unknown;

  constructor(params: {
    message: string;
    code: ErrorCode;
    status?: number;
    details?: unknown;
    cause?: unknown;
  }) {
    super(params.message);
    this.name = "UnifiedApiError";
    this.code = params.code;
    this.status = params.status;
    this.details = params.details;
    this.cause = params.cause;
  }
}

interface CacheEntry {
  expiresAt: number;
  value: UnifiedSuccess<unknown>;
}

interface UnifiedApiClientOptions {
  defaultHeaders?: HeadersMap;
  defaultTimeoutMs?: number;
  cacheEnabled?: boolean;
  defaultCacheTtlMs?: number;
  fetchImpl?: typeof fetch;
}

export class UnifiedApiClient {
  private readonly defaultHeaders: HeadersMap;
  private readonly defaultTimeoutMs: number;
  private readonly cacheEnabled: boolean;
  private readonly defaultCacheTtlMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly cache = new Map<string, CacheEntry>();

  constructor(options: UnifiedApiClientOptions = {}) {
    this.defaultHeaders = options.defaultHeaders ?? {};
    this.defaultTimeoutMs = options.defaultTimeoutMs ?? 15_000;
    this.cacheEnabled = options.cacheEnabled ?? false;
    this.defaultCacheTtlMs = options.defaultCacheTtlMs ?? 30_000;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  async request<T = unknown>(
    options: UnifiedRequestOptions
  ): Promise<UnifiedResponse<T>> {
    const timeoutMs = options.timeoutMs ?? this.defaultTimeoutMs;
    const cacheConfig = {
      enabled: options.cache?.enabled ?? this.cacheEnabled,
      ttlMs: options.cache?.ttlMs ?? this.defaultCacheTtlMs,
      key: options.cache?.key,
    };

    const cacheKey =
      cacheConfig.enabled && this.isCacheable(options)
        ? cacheConfig.key ?? this.buildCacheKey(options)
        : null;

    if (cacheKey) {
      const cached = this.getCached<T>(cacheKey);
      if (cached) {
        return {
          ...cached,
          cached: true,
        };
      }
    }

    try {
      const response = await this.executeRequest<T>(options, timeoutMs);

      if (response.ok && cacheKey) {
        this.setCached(cacheKey, response, cacheConfig.ttlMs);
      }

      return response;
    } catch (err) {
      const wrapped = this.normalizeUnknownError(err);
      return {
        ok: false,
        error: wrapped,
        status: wrapped.status,
        cached: false,
      };
    }
  }

  clearCache(): void {
    this.cache.clear();
  }

  invalidateCache(key: string): void {
    this.cache.delete(key);
  }

  private async executeRequest<T>(
    options: UnifiedRequestOptions,
    timeoutMs: number
  ): Promise<UnifiedResponse<T>> {
    const controller = new AbortController();
    const cleanup = this.linkAbortSignals(controller, options.signal);
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const req = this.buildFetchRequest(options, controller.signal);
      const res = await this.fetchImpl(req.url, req.init);

      const parsed = await this.parseResponseBody(res);

      if (!res.ok) {
        return {
          ok: false,
          error: new UnifiedApiError({
            message: `HTTP ${res.status} ${res.statusText}`,
            code: "HTTP_ERROR",
            status: res.status,
            details: parsed,
          }),
          status: res.status,
          headers: res.headers,
          cached: false,
        };
      }

      if (options.kind === "graphql") {
        const gqlPayload = parsed as {
          data?: T;
          errors?: GraphQLErrorItem[];
        };

        if (Array.isArray(gqlPayload?.errors) && gqlPayload.errors.length > 0) {
          return {
            ok: false,
            error: new UnifiedApiError({
              message: this.formatGraphQLErrorMessage(gqlPayload.errors),
              code: "GRAPHQL_ERROR",
              status: res.status,
              details: gqlPayload.errors,
            }),
            status: res.status,
            headers: res.headers,
            cached: false,
          };
        }

        return {
          ok: true,
          status: res.status,
          data: gqlPayload?.data as T,
          headers: res.headers,
          cached: false,
        };
      }

      return {
        ok: true,
        status: res.status,
        data: parsed as T,
        headers: res.headers,
        cached: false,
      };
    } catch (err) {
      if (this.isAbortError(err)) {
        return {
          ok: false,
          error: new UnifiedApiError({
            message: `Request timed out after ${timeoutMs}ms`,
            code: "TIMEOUT",
            details: { timeoutMs },
            cause: err,
          }),
          cached: false,
        };
      }

      if (err instanceof UnifiedApiError) {
        return {
          ok: false,
          error: err,
          status: err.status,
          cached: false,
        };
      }

      return {
        ok: false,
        error: new UnifiedApiError({
          message: "Network request failed",
          code: "NETWORK_ERROR",
          cause: err,
        }),
        cached: false,
      };
    } finally {
      clearTimeout(timeoutId);
      cleanup();
    }
  }

  private buildFetchRequest(
    options: UnifiedRequestOptions,
    signal: AbortSignal
  ): { url: string; init: RequestInit } {
    const headers: HeadersMap = {
      ...this.defaultHeaders,
      ...options.headers,
    };

    if (options.kind === "rest") {
      const method = options.method ?? "GET";
      const url = this.buildUrlWithQuery(options.url, options.query);
      const hasBody = options.body !== undefined && method !== "GET";

      if (hasBody && !this.hasContentType(headers)) {
        headers["Content-Type"] = "application/json";
      }

      return {
        url,
        init: {
          method,
          headers,
          body: hasBody ? JSON.stringify(options.body) : undefined,
          signal,
        },
      };
    }

    if (!this.hasContentType(headers)) {
      headers["Content-Type"] = "application/json";
    }

    return {
      url: options.url,
      init: {
        method: "POST",
        headers,
        body: JSON.stringify({
          query: options.query,
          variables: options.variables,
          operationName: options.operationName,
        }),
        signal,
      },
    };
  }

  private async parseResponseBody(res: Response): Promise<unknown> {
    if (res.status === 204) return null;

    const contentType = res.headers.get("content-type") ?? "";
    const text = await res.text();

    if (!text) return null;

    if (contentType.includes("application/json")) {
      try {
        return JSON.parse(text);
      } catch (err) {
        throw new UnifiedApiError({
          message: "Failed to parse JSON response",
          code: "PARSE_ERROR",
          status: res.status,
          details: { raw: text.slice(0, 500) },
          cause: err,
        });
      }
    }

    return text;
  }

  private buildUrlWithQuery(url: string, query?: QueryParams): string {
    if (!query || Object.keys(query).length === 0) return url;

    const u = new URL(url);
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue;

      if (Array.isArray(value)) {
        for (const item of value) {
          if (item !== undefined && item !== null) {
            u.searchParams.append(key, String(item));
          }
        }
      } else {
        u.searchParams.set(key, String(value));
      }
    }

    return u.toString();
  }

  private formatGraphQLErrorMessage(errors: GraphQLErrorItem[]): string {
    return errors.map((e) => e.message).join("; ");
  }

  private isAbortError(err: unknown): boolean {
    return err instanceof DOMException && err.name === "AbortError";
  }

  private hasContentType(headers: HeadersMap): boolean {
    return Object.keys(headers).some((k) => k.toLowerCase() === "content-type");
  }

  private linkAbortSignals(
    controller: AbortController,
    externalSignal?: AbortSignal
  ): () => void {
    if (!externalSignal) return () => {};

    const onAbort = () => controller.abort();
    if (externalSignal.aborted) {
      controller.abort();
      return () => {};
    }

    externalSignal.addEventListener("abort", onAbort);
    return () => externalSignal.removeEventListener("abort", onAbort);
  }

  private normalizeUnknownError(err: unknown): UnifiedApiError {
    if (err instanceof UnifiedApiError) return err;

    return new UnifiedApiError({
      message: "Unknown error",
      code: "UNKNOWN_ERROR",
      cause: err,
    });
  }

  private isCacheable(options: UnifiedRequestOptions): boolean {
    if (options.kind === "graphql") return true;
    const method = options.method ?? "GET";
    return method === "GET";
  }

  private buildCacheKey(options: UnifiedRequestOptions): string {
    if (options.kind === "graphql") {
      return JSON.stringify({
        kind: "graphql",
        url: options.url,
        query: options.query,
        variables: options.variables ?? {},
        operationName: options.operationName ?? null,
        headers: this.sortObject({
          ...this.defaultHeaders,
          ...options.headers,
        }),
      });
    }

    return JSON.stringify({
      kind: "rest",
      url: options.url,
      method: options.method ?? "GET",
      query: options.query ?? {},
      body: options.body ?? null,
      headers: this.sortObject({
        ...this.defaultHeaders,
        ...options.headers,
      }),
    });
  }

  private sortObject<T extends Record<string, unknown>>(obj: T): T {
    return Object.keys(obj)
      .sort()
      .reduce((acc, key) => {
        (acc as Record<string, unknown>)[key] = obj[key];
        return acc;
      }, {} as T);
  }

  private getCached<T>(key: string): UnifiedSuccess<T> | null {
    const entry = this.cache.get(key);
    if (!entry) return null;

    if (Date.now() > entry.expiresAt) {
      this.cache.delete(key);
      return null;
    }

    return entry.value as UnifiedSuccess<T>;
  }

  private setCached<T>(
    key: string,
    value: UnifiedSuccess<T>,
    ttlMs: number
  ): void {
    this.cache.set(key, {
      value: {
        ...value,
        cached: false,
      },
      expiresAt: Date.now() + ttlMs,
    });
  }
}