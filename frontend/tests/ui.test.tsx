// End-to-end UI test: renders the real React app in jsdom and drives it against a REAL running
// tradelens server (TL_API). Seed data: one pending order on DEMO1 (see ../../scripts/seed_e2e.py).
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

test("live screener lists every strategy with entry, stop, target and reward-to-risk", async () => {
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Screener" }));
  expect(await screen.findByText("Live screener")).toBeTruthy();
  // every strategy has its own section, matched or not
  for (const name of ["ema_cross", "donchian_breakout", "ema_pullback", "macd_cross", "rsi_reversion", "bollinger_breakout", "new_high_momentum"]) {
    expect(await screen.findByRole("heading", { name: new RegExp(`^${name}`) })).toBeTruthy();
  }
  const dc = (await screen.findByRole("heading", { name: /^donchian_breakout/ })).closest("section")!;
  const row = (await within(dc).findByText("DEMO3")).closest("tr")!;
  expect(within(row).getByText("₹250.00")).toBeTruthy();      // entry
  expect(within(row).getByText("₹240.00")).toBeTruthy();      // stop loss
  expect(within(row).getByText("₹300.00")).toBeTruthy();      // target
  expect(within(row).getByText("1 : 5.0")).toBeTruthy();      // reward : risk
  expect(within(row).getByText("12")).toBeTruthy();           // quantity for the account
  expect(within(row).getByText("Proven")).toBeTruthy();
  expect(within(row).getByText("Live, provisional")).toBeTruthy();
  const ec = (await screen.findByRole("heading", { name: /^ema_cross/ })).closest("section")!;
  expect(within(ec).getByText("Faded")).toBeTruthy();
  expect(within(ec).getByText("The stop is too wide for your per-trade risk budget")).toBeTruthy();
  expect(within(dc).queryByText(/No stock matches/)).toBeNull();
  const empty = (await screen.findByRole("heading", { name: /^rsi_reversion/ })).closest("section")!;
  expect(within(empty).getByText("No stock matches right now.")).toBeTruthy();
  // demo mode has no live price source: the button is honestly disabled
  expect((screen.getByRole("button", { name: "Check now" }) as HTMLButtonElement).disabled).toBe(true);
});

test("market context, held-back signals with their flags, and the watchlist", async () => {
  render(<App />);
  // the context bar is on every page (in demo mode the benchmark is synthetic like everything else)
  expect(await screen.findByText(/^Market: (Uptrend|Downtrend|Sideways)/)).toBeTruthy();
  expect(await screen.findByText(/Gate: advisory/)).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Signals" }));
  const row = (await screen.findAllByText("DEMO3")).map((e) => e.closest("tr")!).find((r) => r.textContent!.includes("macd_cross"))!;
  expect(within(row).getAllByText("Held back").length).toBe(2);                 // the gate verdict and the status
  expect(within(row).getAllByText("It has not passed the stability check on this stock").length).toBeGreaterThan(0);
  expect(within(row).getByText("market regime unsuited")).toBeTruthy();
  expect(within(row).getByText("thinly traded")).toBeTruthy();
  expect(within(row).getByText("market down/calm")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Screener" }));
  const wl = (await screen.findByText(/Watchlist: close to triggering/)).closest("section")!;
  const w = (await within(wl).findByText("DEMO2")).closest("tr")!;
  expect(within(w).getByText("donchian_breakout")).toBeTruthy();
  expect(within(w).getByText("+1.2%")).toBeTruthy();
  expect(within(w).getByText(/a close above it triggers/)).toBeTruthy();
  // flags on a live match
  const dc = (await screen.findByRole("heading", { name: /^donchian_breakout/ })).closest("section")!;
  expect(await within(dc).findByText("earnings soon")).toBeTruthy();
  expect(within(dc).getByText("thinly traded")).toBeTruthy();
});

test("strategy lifecycle, event calendar and position advice", async () => {
  render(<App />);
  // lifecycle: the preset saved earlier starts as a draft and can only be retired until a backtest validates it
  fireEvent.click(await screen.findByRole("button", { name: "Strategies" }));
  const table = (await screen.findByText("Where each strategy stands")).closest("section")!;
  const quick = (await within(table).findByText("quick")).closest("tr")!;
  expect(within(quick).getByText("Draft")).toBeTruthy();
  expect(within(quick).queryByRole("button", { name: "Switch on" })).toBeNull();     // no shortcut past the evidence
  fireEvent.click(within(quick).getByRole("button", { name: "Retire" }));
  fireEvent.click(await within(table).findByRole("button", { name: "Restore as draft" }));   // only a retired row offers this
  await waitFor(() => expect(within(table).queryByRole("button", { name: "Restore as draft" })).toBeNull());
  expect(within(within(table).getByText("quick").closest("tr")!).getByText("Draft")).toBeTruthy();
  expect(within(table).getAllByText("Active").length).toBe(10);                      // the 10 built-ins (7 classic + 3 SMC) are active

  // events: add one, see it, remove it
  fireEvent.click(screen.getByRole("button", { name: "Data" }));
  const soon = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10);
  fireEvent.change(await screen.findByLabelText(/Symbol \(or \* for all\)/), { target: { value: "tcs" } });
  fireEvent.change(screen.getByLabelText("Date"), { target: { value: soon } });
  fireEvent.click(screen.getByRole("button", { name: "Add event" }));
  const ev = (await screen.findByText("TCS")).closest("tr")!;
  expect(within(ev).getByText("earnings")).toBeTruthy();
  fireEvent.click(within(ev).getByRole("button", { name: "Remove" }));
  await waitFor(() => expect(screen.queryByText("TCS")).toBeNull());

  // advice: the position opened in the first test has had no new bar since its signal, so no strategy wants out
  fireEvent.click(screen.getByRole("button", { name: "Desk" }));
  expect(await screen.findByText("Strategy advice on open positions")).toBeTruthy();
  fireEvent.click(await screen.findByRole("button", { name: "Ask the strategies now" }));
  expect(await screen.findByText("No strategy wants out of anything you hold.")).toBeTruthy();
});
