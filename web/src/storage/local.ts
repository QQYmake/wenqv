import type { ChatMessage, ProviderConfigSet, RuntimeContext, Session } from "../types";

const DATABASE_NAME = "blue-lake-local";
const DATABASE_VERSION = 1;
const META = "meta";
const SESSIONS = "sessions";
const TIMELINES = "timelines";
const RUNTIME = "runtime";
const PROVIDER_KEYS = "provider_keys";
const PROVIDER_CONFIG = "provider_config";
const PROVIDER_KEY_ID = "provider-aes-gcm-key";
const PROVIDER_CONFIG_ID = "provider-config";

type KeyRecord = { id: string; key: CryptoKey };
type EncryptedProviderRecord = { id: string; iv: ArrayBuffer; ciphertext: ArrayBuffer };
type TimelineRecord = { sessionId: string; messages: ChatMessage[] };
type RuntimeRecord = { sessionId: string; context: RuntimeContext };

export class LocalPrivacyError extends Error {
  constructor(public readonly code: "storage_unavailable" | "crypto_unavailable" | "provider_reconfigure") {
    super(code);
    this.name = "LocalPrivacyError";
  }
}

function requirePlatform(): void {
  if (!globalThis.indexedDB) throw new LocalPrivacyError("storage_unavailable");
  if (!globalThis.crypto?.subtle || !globalThis.crypto.randomUUID) {
    throw new LocalPrivacyError("crypto_unavailable");
  }
}

function openDatabase(): Promise<IDBDatabase> {
  requirePlatform();
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(META)) database.createObjectStore(META, { keyPath: "id" });
      if (!database.objectStoreNames.contains(SESSIONS)) database.createObjectStore(SESSIONS, { keyPath: "id" });
      if (!database.objectStoreNames.contains(TIMELINES)) database.createObjectStore(TIMELINES, { keyPath: "sessionId" });
      if (!database.objectStoreNames.contains(RUNTIME)) database.createObjectStore(RUNTIME, { keyPath: "sessionId" });
      if (!database.objectStoreNames.contains(PROVIDER_KEYS)) database.createObjectStore(PROVIDER_KEYS, { keyPath: "id" });
      if (!database.objectStoreNames.contains(PROVIDER_CONFIG)) database.createObjectStore(PROVIDER_CONFIG, { keyPath: "id" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new LocalPrivacyError("storage_unavailable"));
    request.onblocked = () => reject(new LocalPrivacyError("storage_unavailable"));
  });
}

function requestValue<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new LocalPrivacyError("storage_unavailable"));
  });
}

function complete(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(new LocalPrivacyError("storage_unavailable"));
    transaction.onabort = () => reject(new LocalPrivacyError("storage_unavailable"));
  });
}

function emptyContext(): RuntimeContext {
  return { messages: [], active_skills: [] };
}

function now(): string {
  return new Date().toISOString();
}

function normalSession(value: Session): Session {
  return {
    id: String(value.id),
    title: String(value.title || "新对话"),
    created_at: value.created_at,
    updated_at: value.updated_at,
    message_count: Number(value.message_count ?? 0),
  };
}

export async function getWorkspaceId(): Promise<string> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(META, "readwrite");
    const store = transaction.objectStore(META);
    const existing = await requestValue(store.get("workspace_id") as IDBRequest<{ id: string; value?: unknown } | undefined>);
    if (typeof existing?.value === "string" && existing.value) {
      await complete(transaction);
      return existing.value;
    }
    const workspaceId = crypto.randomUUID();
    store.put({ id: "workspace_id", value: workspaceId });
    await complete(transaction);
    return workspaceId;
  } finally {
    database.close();
  }
}

export async function listSessions(): Promise<Session[]> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(SESSIONS, "readonly");
    const values = await requestValue(transaction.objectStore(SESSIONS).getAll() as IDBRequest<Session[]>);
    await complete(transaction);
    return values.map(normalSession).sort((left, right) => String(right.updated_at ?? "").localeCompare(String(left.updated_at ?? "")));
  } finally {
    database.close();
  }
}

export async function createSession(title = "新对话"): Promise<Session> {
  const session: Session = { id: crypto.randomUUID(), title, created_at: now(), updated_at: now(), message_count: 0 };
  const database = await openDatabase();
  try {
    const transaction = database.transaction(SESSIONS, "readwrite");
    transaction.objectStore(SESSIONS).put(session);
    await complete(transaction);
    return session;
  } finally {
    database.close();
  }
}

export async function renameSession(id: string, title: string): Promise<Session | null> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(SESSIONS, "readwrite");
    const store = transaction.objectStore(SESSIONS);
    const existing = await requestValue(store.get(id) as IDBRequest<Session | undefined>);
    if (!existing) {
      await complete(transaction);
      return null;
    }
    const updated = { ...normalSession(existing), title: title.trim() || "新对话", updated_at: now() };
    store.put(updated);
    await complete(transaction);
    return updated;
  } finally {
    database.close();
  }
}

export async function deleteSession(id: string): Promise<void> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction([SESSIONS, TIMELINES, RUNTIME], "readwrite");
    transaction.objectStore(SESSIONS).delete(id);
    transaction.objectStore(TIMELINES).delete(id);
    transaction.objectStore(RUNTIME).delete(id);
    await complete(transaction);
  } finally {
    database.close();
  }
}

export async function loadConversation(sessionId: string): Promise<{ messages: ChatMessage[]; runtimeContext: RuntimeContext }> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction([TIMELINES, RUNTIME], "readonly");
    const timeline = await requestValue(transaction.objectStore(TIMELINES).get(sessionId) as IDBRequest<TimelineRecord | undefined>);
    const runtime = await requestValue(transaction.objectStore(RUNTIME).get(sessionId) as IDBRequest<RuntimeRecord | undefined>);
    await complete(transaction);
    return { messages: timeline?.messages ?? [], runtimeContext: runtime?.context ?? emptyContext() };
  } finally {
    database.close();
  }
}

export async function saveConversation(
  sessionId: string,
  messages: ChatMessage[],
  runtimeContext: RuntimeContext,
): Promise<Session | null> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction([SESSIONS, TIMELINES, RUNTIME], "readwrite");
    const sessionStore = transaction.objectStore(SESSIONS);
    const session = await requestValue(sessionStore.get(sessionId) as IDBRequest<Session | undefined>);
    if (session) {
      const updated = {
        ...normalSession(session),
        updated_at: now(),
        message_count: messages.filter((message) => message.role === "user" || message.role === "assistant").length,
      };
      sessionStore.put(updated);
    }
    transaction.objectStore(TIMELINES).put({ sessionId, messages } satisfies TimelineRecord);
    transaction.objectStore(RUNTIME).put({ sessionId, context: runtimeContext } satisfies RuntimeRecord);
    await complete(transaction);
    return session
      ? { ...normalSession(session), updated_at: now(), message_count: messages.filter((message) => message.role === "user" || message.role === "assistant").length }
      : null;
  } finally {
    database.close();
  }
}

async function providerKey(database: IDBDatabase): Promise<CryptoKey> {
  const transaction = database.transaction(PROVIDER_KEYS, "readwrite");
  const store = transaction.objectStore(PROVIDER_KEYS);
  const saved = await requestValue(store.get(PROVIDER_KEY_ID) as IDBRequest<KeyRecord | undefined>);
  if (saved?.key) {
    await complete(transaction);
    return saved.key;
  }
  try {
    const key = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    store.put({ id: PROVIDER_KEY_ID, key } satisfies KeyRecord);
    await complete(transaction);
    return key;
  } catch {
    transaction.abort();
    throw new LocalPrivacyError("crypto_unavailable");
  }
}

function providerAad(workspaceId: string): ArrayBuffer {
  const encoded = new TextEncoder().encode(workspaceId);
  return encoded.buffer as ArrayBuffer;
}

export async function saveProviderConfig(workspaceId: string, config: ProviderConfigSet): Promise<void> {
  const database = await openDatabase();
  try {
    const key = await providerKey(database);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    let ciphertext: ArrayBuffer;
    try {
      ciphertext = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv: iv.buffer as ArrayBuffer, additionalData: providerAad(workspaceId) },
        key,
        new TextEncoder().encode(JSON.stringify(config)),
      );
    } catch {
      throw new LocalPrivacyError("crypto_unavailable");
    }
    const transaction = database.transaction(PROVIDER_CONFIG, "readwrite");
    transaction.objectStore(PROVIDER_CONFIG).put({
      id: PROVIDER_CONFIG_ID,
      iv: iv.buffer.slice(0) as ArrayBuffer,
      ciphertext,
    } satisfies EncryptedProviderRecord);
    await complete(transaction);
  } finally {
    database.close();
  }
}

export async function loadProviderConfig(workspaceId: string): Promise<ProviderConfigSet | null> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(PROVIDER_CONFIG, "readonly");
    const saved = await requestValue(
      transaction.objectStore(PROVIDER_CONFIG).get(PROVIDER_CONFIG_ID) as IDBRequest<EncryptedProviderRecord | undefined>,
    );
    await complete(transaction);
    if (!saved) return null;
    let key: CryptoKey;
    try {
      const keyTransaction = database.transaction(PROVIDER_KEYS, "readonly");
      const keyRecord = await requestValue(
        keyTransaction.objectStore(PROVIDER_KEYS).get(PROVIDER_KEY_ID) as IDBRequest<KeyRecord | undefined>,
      );
      await complete(keyTransaction);
      if (!keyRecord?.key) throw new Error("missing key");
      key = keyRecord.key;
      const plaintext = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: saved.iv, additionalData: providerAad(workspaceId) },
        key,
        saved.ciphertext,
      );
      const parsed = JSON.parse(new TextDecoder().decode(plaintext)) as ProviderConfigSet;
      if (!parsed?.main?.base_url || !parsed.main.api_key || !parsed.main.model) throw new Error("invalid config");
      return parsed;
    } catch {
      // There is intentionally no plaintext or server fallback when a key was
      // cleared, becomes unusable, or ciphertext is corrupted.
      throw new LocalPrivacyError("provider_reconfigure");
    }
  } finally {
    database.close();
  }
}

export function blankProviderConfig(): ProviderConfigSet {
  return {
    main: { base_url: "", api_key: "", model: "" },
    summary: { base_url: "", api_key: "", model: "" },
  };
}
