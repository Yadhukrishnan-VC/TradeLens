// End-to-end UI test: renders the real React app in jsdom and drives it against a REAL running
// tradelite server (TL_API). Seed data: one pending order on DEMO1 (see ../../scripts/seed_e2e.py).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, expect, test, vi } from "vitest";
import App from "../src/App";

const API = process.env.TL_API ?? "http://127.0.0.1:8766";

beforeAll(() => {
  const real = globalThis.fetch;
  globalThis.fetch = ((url: string, init?: RequestInit) => real(API + url, init)) as typeof fetch;
});
afterEach(cleanup);

test("full desk workflow against the real backend", async () => {
  render(<App />);

  // risk strip loads real account data
  expect(await screen.findByText("Account equity")).toBeTruthy();
  expect(await screen.findByText("₹1,00,000")).toBeTruthy();
  expect(await screen.findByText(/Demo data: prices are synthetic/)).toBeTruthy();

  // the seeded pending order is waiting for approval, with its risk figures
  const heading = await screen.findByText(/^Buy \d+ DEMO1$/);
  const row = heading.closest("li")!;
  expect(within(row).getByText("Risk if stopped")).toBeTruthy();
  expect(within(row).getByText("Proven")).toBeTruthy();   // its strategy+stock pair has a good track record
  fireEvent.click(within(row).getByText("Approve and place"));
  expect(await screen.findByText(/Bought \d+ DEMO1 at ₹/)).toBeTruthy();

  // the position now exists and capital deployed moved off zero
  await waitFor(() => expect(screen.getByRole("table", {})).toBeTruthy());
  expect(await screen.findByText("Capital in trade")).toBeTruthy();
  await waitFor(() => {
    const meter = screen.getByRole("meter", { name: "Capital deployed" });
    expect(Number(meter.getAttribute("aria-valuenow"))).toBeGreaterThan(0);
  });
  expect(screen.getByText(/1 of 5 in use/)).toBeTruthy();

  // backtest: run, see verdict, metrics, chart, strategy map
  fireEvent.click(screen.getByRole("button", { name: "Backtests" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "Run backtest" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "Run backtest" }));
  expect(await screen.findByText("Net profit after costs")).toBeTruthy();
  expect(await screen.findByRole("img", { name: /Cumulative net profit after \d+ trades/ })).toBeTruthy();
  expect(await screen.findByText("Does it hold up in both halves of the history?")).toBeTruthy();
  const map = (await screen.findByText("Strategy map")).closest("section")!;
  await waitFor(() => expect(within(map).getAllByText("DEMO1").length).toBeGreaterThan(0));

  // scan: reports an outcome in plain words
  fireEvent.click(screen.getByRole("button", { name: "Signals" }));
  fireEvent.click(await screen.findByRole("button", { name: "Scan all symbols" }));
  expect(await screen.findByText(/No new signals on the latest bars|new signals?:/)).toBeTruthy();

  // positions page shows the open trade count
  fireEvent.click(screen.getByRole("button", { name: "Positions" }));
  expect(await screen.findByText("Results so far")).toBeTruthy();

  // kill switch: halt shows the warning, resume (confirmed) clears it
  fireEvent.click(screen.getByRole("button", { name: "Halt all trading" }));
  expect(await screen.findByText(/Trading is halted\. New entries are blocked/)).toBeTruthy();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "Resume trading" }));
  await waitFor(() => expect(screen.queryByText(/Trading is halted\. New entries are blocked/)).toBeNull());
});

test("track record, custom settings and data pages", async () => {
  render(<App />);

  // what worked: the proven pair, with its dates
  fireEvent.click(await screen.findByRole("button", { name: "Track Record" }));
  const proven = (await screen.findByText("What worked, on which stock")).closest("section")!;
  expect(await within(proven).findByText("DEMO2")).toBeTruthy();
  expect(within(proven).getByText("macd_cross")).toBeTruthy();
  expect(within(proven).getByText("1.70")).toBeTruthy();

  // customise a strategy, save it, run it: it is tracked separately from the built-in settings
  fireEvent.click(screen.getByRole("button", { name: "Backtests" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "Run backtest" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText("Strategy"), { target: { value: "macd_cross" } });
  fireEvent.change(await screen.findByLabelText("fast"), { target: { value: "8" } });
  fireEvent.change(screen.getByLabelText(/Save these settings as/), { target: { value: "quick" } });
  fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
  await waitFor(() => expect((screen.getByLabelText("Settings") as HTMLSelectElement).value).toBe("quick"));
  fireEvent.click(screen.getByRole("button", { name: "Run backtest" }));
  expect(await screen.findByText("macd_cross (quick) on DEMO1")).toBeTruthy();
  const map = (await screen.findByText("Strategy map")).closest("section")!;
  await waitFor(() => expect(within(map).getAllByText("quick").length).toBeGreaterThan(0));

  // data page: nothing stored in demo mode, and it says how to fetch
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  expect(await screen.findByText("Fetch historical prices")).toBeTruthy();
  expect(await screen.findByText(/No prices stored yet/)).toBeTruthy();
});
