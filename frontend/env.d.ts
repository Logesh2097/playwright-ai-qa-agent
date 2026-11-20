/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the versioned API. Defaults to the same-origin `/api/v1`. */
  readonly VITE_API_BASE_URL?: string
  /** Name shown in the application shell. */
  readonly VITE_APP_NAME?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
