// The dashboard asks for the API token when the server answers 401, and sends it as a Bearer header afterwards.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "../src/App";
import { getToken } from "../src/api";

beforeEach(() => localStorage.clear());
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test("401 shows the token form; the saved token is sent on the next request", async () => {
  const seen: Array<string | undefined> = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    const auth = (init?.headers as Record<string, string> | undefined)?.authorization;
    seen.push(auth);
    return new Response(JSON.stringify({ detail: "missing or invalid API token" }), { status: 401 });
  }));
  Object.defineProperty(window, "location", { value: { ...window.location, reload: vi.fn() }, writable: true });

  render(<App />);
  const input = await screen.findByLabelText("API token");
  fireEvent.change(input, { target: { value: "  s3cret-token-value-1234567890  " } });
  fireEvent.click(screen.getByText("Unlock"));
  await waitFor(() => expect(getToken()).toBe("s3cret-token-value-1234567890"));   // trimmed, stored
  expect(seen.every((a) => a === undefined)).toBe(true);                           // nothing was sent before it existed

  cleanup();
  seen.length = 0;
  render(<App />);
  await waitFor(() => expect(seen.length).toBeGreaterThan(0));
  expect(seen[0]).toBe("Bearer s3cret-token-value-1234567890");
});
