/** Compare the four workbench pages in real Chromium against the pump-only service. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');
const base = process.env.STYLE_QA_URL || 'http://127.0.0.1:8002';
const output = path.resolve(process.env.STYLE_QA_OUTPUT || 'docs/validation/page-style-2026-09-10/comparison');
const pages = [
  ['explore', '看清设备之间的联系。', '.graph-workspace'],
  ['search', '提一个问题，找到依据。', '.search-card'],
  ['sources', '知识库的每一份依据。', '.source-card'],
  ['build', '把资料变成有据可查的知识。', '.instance-workspace'],
];
(async () => {
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname));
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ deviceScaleFactor: 1 });
  const page = await context.newPage();
  const results = { status: 'running', dataScope: null, observations: [], errors: [], blockedRequests: [], failedResponses: [] };
  page.on('pageerror', error => results.errors.push(error.message));
  page.on('response', response => {
    if (response.status() >= 400) results.failedResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
  });
  try {
    const bootstrap = await context.request.get(base + '/playground/bootstrap');
    assert.equal(bootstrap.ok(), true);
    results.dataScope = (await bootstrap.json()).data_scope;
    assert.equal(results.dataScope, 'pump-only');
    const readPosts = new Set(['/playground/session', '/v1/knowledge/graph:query', '/v1/knowledge/sources:query', '/v1/industrial/sources:query']);
    await context.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      const readGet = ['GET', 'HEAD'].includes(req.method()) && (
        url.pathname === '/industrial' || url.pathname.startsWith('/industrial/assets/') ||
        ['/favicon.ico', '/playground/bootstrap'].includes(url.pathname) ||
        /^\/v1\/(?:ontologies|knowledge\/(?:construction-jobs|review-queue|publication-candidates|publications|entity-resolution|review-assessments))(?:\/|$)/.test(url.pathname)
      );
      if (url.origin === new URL(base).origin && (readGet || (req.method() === 'POST' && readPosts.has(url.pathname)))) return route.continue();
      results.blockedRequests.push({ method: req.method(), path: url.pathname });
      return route.abort('blockedbyclient');
    });
    for (const [width, height] of [[1366, 900], [1440, 900], [1920, 1080]]) {
      await page.setViewportSize({ width, height });
      await page.goto(base + '/industrial');
      await page.waitForFunction(() => document.querySelector('#node-count')?.textContent !== '—' && document.querySelector('#graph-placeholder')?.hidden);
      let reference;
      for (const [id, title, card] of pages) {
        await page.locator('.rail [data-panel="' + id + '"]').click();
        if (id === 'sources') await page.waitForFunction(() => document.querySelector('#sources-list .source-card') && !document.querySelector('#sources-summary').textContent.includes('正在读取'));
        if (id === 'build') await page.waitForFunction(() => document.querySelector('#document-tbox')?.value && !document.querySelector('#foundation-status').textContent.includes('正在读取'));
        await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert.equal(await page.locator('#panel-' + id + ' > .page-heading h1').textContent(), title);
        const metrics = await page.evaluate(({ id, card }) => {
          const panel = document.querySelector('#panel-' + id);
          const heading = panel.querySelector('.page-heading');
          const h1 = heading.querySelector('h1'), eyebrow = heading.querySelector('.eyebrow');
          const style = getComputedStyle(h1), eyebrowStyle = getComputedStyle(eyebrow);
          const surface = panel.querySelector(card), surfaceStyle = getComputedStyle(surface);
          const rect = h1.getBoundingClientRect(), surfaceRect = surface.getBoundingClientRect();
          return {
            id, width: innerWidth, height: innerHeight, pageWidth: document.documentElement.scrollWidth,
            heading: { x: rect.x, y: rect.y, fontSize: style.fontSize, lineHeight: style.lineHeight, weight: style.fontWeight, color: style.color, eyebrowSize: eyebrowStyle.fontSize, eyebrowColor: eyebrowStyle.color },
            surface: { x: surfaceRect.x, width: surfaceRect.width, radius: surfaceStyle.borderRadius, border: surfaceStyle.borderColor, background: surfaceStyle.backgroundColor },
          };
        }, { id, card });
        results.observations.push(metrics);
        assert.ok(metrics.pageWidth <= width, id + ': no horizontal overflow');
        assert.ok(Math.abs(metrics.surface.x - metrics.heading.x) <= 1, id + ': card aligns with page heading');
        if (!reference) reference = metrics;
        assert.deepEqual(metrics.heading, reference.heading, id + ': consistent heading position and typography');
        for (const key of ['radius', 'border', 'background']) assert.equal(metrics.surface[key], reference.surface[key], id + ': shared surface ' + key);
        await page.screenshot({ path: path.join(output, `${width}-${id}.png`) });
      }
      await page.locator('#build-tab-ontology').click();
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
      await page.screenshot({ path: path.join(output, `${width}-ontology.png`) });
    }
    assert.deepEqual(results.errors, []);
    assert.deepEqual(results.blockedRequests, []);
    assert.deepEqual(results.failedResponses, []);
    results.status = 'passed';
    console.log(JSON.stringify({ status: results.status, screenshots: 15, output }));
  } catch (error) {
    results.status = 'failed';
    results.failure = error.message;
    await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {});
    throw error;
  } finally {
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify(results, null, 2) + '\n');
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
