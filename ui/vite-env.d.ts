/// <reference types="vite/client" />

declare const __BUILD_SHA__: string;

interface Window {
  __HEM_CONFIG__?: {
    apiBase: string;
    bearer: string | null;
    buildSha: string;
  };
}
