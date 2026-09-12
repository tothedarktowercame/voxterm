import { chromium } from "playwright";
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
const page = await ctx.newPage();
await page.goto("http://localhost:3999/story/vsatlatarium?anthology=34", { waitUntil: "domcontentloaded", timeout: 90000 });
await page.waitForTimeout(7000);
await page.locator("#vsat-demo-bar").screenshot({ path: "/tmp/bar-idle.png" });
// tap Next twice, as a thumb would
await page.tap("#vsat-demo-tour-next");
await page.waitForTimeout(400);
await page.locator("#vsat-demo-bar").screenshot({ path: "/tmp/bar-step1.png" });
await page.tap("#vsat-demo-tour-next");
await page.waitForTimeout(400);
const t = await page.evaluate(() => document.getElementById("vsat-demo-tip").textContent.trim().slice(0, 60));
const box = await page.evaluate(() => { const b = document.getElementById("vsat-demo-tour-next").getBoundingClientRect(); return { w: Math.round(b.width), h: Math.round(b.height) }; });
console.log("after two taps:", JSON.stringify(t));
console.log("Next button size:", JSON.stringify(box));
await browser.close();
