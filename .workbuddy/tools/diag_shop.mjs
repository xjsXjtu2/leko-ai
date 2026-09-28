// 诊断：找出商品页的店铺入口与「搜本店」输入框
import { createRequire } from 'node:module';
const require = createRequire('/Users/xjs/.workbuddy/binaries/node/workspace/');
const { chromium } = require('playwright-core');

const browser = await chromium.connectOverCDP(process.env.CDP_URL || 'http://127.0.0.1:9222');
const ctx = browser.contexts()[0];
let page = ctx.pages().find((p) => !p.isClosed() && p.url().startsWith('http')) || (await ctx.newPage());

await page.goto('https://item.taobao.com/item.htm?id=649050075825', { waitUntil: 'domcontentloaded' }).catch(() => {});
await page.waitForTimeout(4000);

const diag = await page.evaluate(() => {
  const inputs = [...document.querySelectorAll('input')].map((i) => ({
    id: i.id,
    name: i.name,
    ph: i.placeholder,
    cls: (i.className || '').slice(0, 60),
  })).filter((i) => i.ph || i.name || i.id);

  const shopLinks = [...document.querySelectorAll('a')]
    .map((a) => a.href)
    .filter((h) => /shop\d+\.taobao\.com|shop\.taobao\.com|\/shop\d/.test(h))
    .slice(0, 12);

  const htmlShop = (document.documentElement.innerHTML.match(/shopId["':\s]+(\d{6,})/gi) || []).slice(0, 8);

  return { inputs, shopLinks, htmlShop, url: location.href };
});

console.log('URL:', diag.url);
console.log('--- INPUTS ---');
diag.inputs.forEach((i) => console.log(JSON.stringify(i)));
console.log('--- SHOP LINKS ---');
diag.shopLinks.forEach((l) => console.log(l));
console.log('--- HTML shopId hits ---');
diag.htmlShop.forEach((l) => console.log(l));

await browser.close();
