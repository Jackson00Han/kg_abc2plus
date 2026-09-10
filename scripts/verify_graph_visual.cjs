/** Real browser checks against the running pump-only workbench. No corpus writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('playwright');
const base = process.env.GRAPH_QA_URL || 'http://127.0.0.1:8002';
const output = path.resolve(process.env.GRAPH_QA_OUTPUT || 'docs/validation/graph-visual-2026-09-10');
const observations = [];
(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    const config = await (await context.request.get(base + '/playground/bootstrap')).json();
    assert.equal(config.data_scope, 'pump-only');
    async function ready() {
      await page.waitForFunction(() => document.querySelector('#node-count')?.textContent !== '—' &&
        document.querySelector('#graph-placeholder')?.hidden);
    }
    async function shot(name) { await page.screenshot({ path: path.join(output, name + '.png') }); }
    async function evidenceReady() {
      await page.waitForFunction(() => !document.querySelector('#inspector-content').textContent.includes('正在核对来源'));
      assert.ok((await page.locator('#inspector-content').innerText()).includes('来源与事实依据'));
    }
    async function metrics(name) {
      const result = await page.evaluate(() => {
        const canvas = document.querySelector('#graph-canvas');
        const cy = canvas._cyreg.cy;
        const rect = canvas.getBoundingClientRect();
        return { width: innerWidth, height: innerHeight, pageWidth: document.documentElement.scrollWidth,
          canvasTop: rect.top, canvasHeight: rect.height, zoom: cy.zoom(), nodes: cy.nodes().length,
          minimumNodeFont: Math.min(...cy.nodes().map(n => n.numericStyle('font-size') * cy.zoom())),
          fullscreen: !!document.fullscreenElement, nodeBounds: cy.nodes().renderedBoundingBox(), canvasWidth: rect.width };
      });
      observations.push({ name, ...result });
      assert.ok(result.pageWidth <= result.width, name + ' horizontal overflow');
      return result;
    }
    async function clickGraph(kind, label) {
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const point = await page.evaluate(({ kind, label }) => {
        const canvas = document.querySelector('#graph-canvas'), cy = canvas._cyreg.cy;
        const item = kind === 'node' ? cy.nodes().filter(n => n.data('label') === label)[0] : cy.edges()[0];
        if (!item) throw Error('Pump fixture target missing');
        const pos = kind === 'node' ? item.renderedPosition() : item.renderedMidpoint();
        return { x: pos.x, y: pos.y };
      }, { kind, label });
      await page.locator('#graph-canvas').click({ position: point });
      await page.waitForFunction(() => !document.querySelector('#graph-inspector').hidden);
    }
    async function reset() { await page.getByRole('button', { name: '恢复全图', exact: true }).click(); }
    async function queryFrom(action) {
      const response = page.waitForResponse(r => r.url().endsWith('/v1/knowledge/graph:query') && r.request().method() === 'POST');
      await action();
      const result = await response;
      assert.equal(result.ok(), true);
      await ready();
      return result.request().postDataJSON();
    }
    const initialGraphResponse = page.waitForResponse(r => r.url().endsWith('/v1/knowledge/graph:query') && r.request().method() === 'POST');
    await page.goto(base + '/industrial'); await ready();
    const schema = (await (await initialGraphResponse).json()).schema;
    assert.equal(await page.locator('#panel-explore > .scope-bar').count(), 0);
    for (const id of ['trust', 'graph-relation-filter', 'reload-graph']) {
      assert.equal(await page.locator('.graph-toolbar #' + id).isVisible(), true);
    }
    assert.equal((await page.locator('#reload-graph').innerText()).trim(), '');
    const declared = new Set(schema.relationship_types.map(r => r.name));
    const declaredHierarchies = (schema.hierarchies || []).filter(h => declared.has(h.relationship_type));
    assert.equal(await page.locator('#graph-relation-filter optgroup[label="本体层级"] option').count(), declaredHierarchies.length);
    assert.equal(await page.locator('#graph-relation-filter optgroup[label="关系类型"] option').count(), declared.size);
    const trustQuery = await queryFrom(() => page.locator('#trust').selectOption('AUTHORITATIVE_ONLY'));
    assert.equal(trustQuery.trust_policy, 'AUTHORITATIVE_ONLY');
    const relation = await page.locator('#graph-relation-filter optgroup[label="关系类型"] option').first().getAttribute('value');
    const filterQuery = await queryFrom(() => page.locator('#graph-relation-filter').selectOption(relation));
    assert.deepEqual(filterQuery.predicates, [relation.slice('relation:'.length)]);
    const refreshQuery = await queryFrom(() => page.getByRole('button', { name: '刷新图谱', exact: true }).click());
    assert.equal(refreshQuery.trust_policy, 'AUTHORITATIVE_ONLY');
    assert.deepEqual(refreshQuery.predicates, filterQuery.predicates);
    await shot('00-scope-filtered');
    await queryFrom(() => page.locator('#graph-relation-filter').selectOption(''));
    await queryFrom(() => page.locator('#trust').selectOption('PUBLISHED_SECONDARY_INCLUSIVE'));
    assert.equal(await page.locator('#global-message').isVisible(), false);
    const desktop = await metrics('desktop');
    assert.ok(desktop.canvasTop < 420);
    assert.ok(desktop.minimumNodeFont >= 12);
    await shot('01-desktop');
    await page.locator('.graph-node-search').fill('循环水泵');
    await page.locator('.graph-node-search').press('Enter');
    await page.waitForFunction(() => !document.querySelector('#graph-inspector').hidden);
    await reset();
    await page.getByRole('button', { name: '关系标签', exact: true }).click();
    assert.equal(await page.getByRole('button', { name: '关系标签', exact: true }).getAttribute('aria-pressed'), 'false');
    assert.equal(await page.evaluate(() => document.querySelector('#graph-canvas')._cyreg.cy.edges().every(e=>e.hasClass('labels-hidden'))), true);
    await page.getByRole('button', { name: '关系标签', exact: true }).click();
    await page.getByRole('button', { name: '当前层级布局，切换为网络布局', exact: true }).click();
    await page.getByRole('button', { name: '当前网络布局，切换为层级布局', exact: true }).click();
    await page.getByRole('button', { name: '适应画布', exact: true }).click();
    const initialScroll = await page.evaluate(() => scrollY);
    await clickGraph('node', '循环水泵');
    assert.equal(await page.evaluate(() => scrollY), initialScroll);
    assert.equal(await page.getByRole('button', { name: '查看详情 ↓', exact: true }).isVisible(), true);
    await shot('02-desktop-selected');
    await page.getByRole('button', { name: '查看详情 ↓', exact: true }).click();
    await page.waitForFunction(() => document.activeElement?.id === 'inspector-summary');
    await evidenceReady();
    await shot('03-desktop-details');
    await page.locator('#inspector-summary').click();
    assert.equal(await page.locator('#graph-inspector').evaluate(e => e.open), false);
    await reset();
    assert.equal(await page.locator('#graph-inspector').isVisible(), false);
    await clickGraph('edge');
    assert.equal(await page.locator('#graph-inspector').evaluate(e => e.open), true);
    await page.getByRole('button', { name: '本体模型', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#graph-relation-filter').disabled);
    assert.equal(await page.locator('#graph-inspector').isVisible(), false);
    assert.equal(await page.locator('.graph-key').isVisible(), false);
    assert.equal(await page.getByRole('button', { name: '关系标签', exact: true }).getAttribute('aria-pressed'), 'false');
    await shot('04-ontology');
    await clickGraph('edge');
    assert.equal(await page.evaluate(() => document.querySelector('#graph-canvas')._cyreg.cy.edges(':selected').every(e => !!e.style('label'))), true);
    await shot('04-ontology-selected');
    await page.getByRole('button', { name: '实例图谱', exact: true }).click(); await ready();
    await page.waitForFunction(() => !document.querySelector('#graph-relation-filter').disabled);
    await page.getByRole('button', { name: '全屏', exact: true }).click();
    await page.waitForFunction(() => !!document.fullscreenElement);
    for (const id of ['trust', 'graph-relation-filter', 'reload-graph']) {
      assert.equal(await page.locator('#' + id).evaluate(el => document.fullscreenElement.contains(el)), true);
    }
    await queryFrom(() => page.getByRole('button', { name: '刷新图谱', exact: true }).click());
    await clickGraph('node', '循环水泵');
    await page.waitForFunction(() => {
      const el=document.querySelector('#graph-canvas'), cy=el._cyreg.cy, b=cy.nodes().renderedBoundingBox();
      return b.x1>=0 && b.y1>=0 && b.x2<=el.clientWidth && b.y2<=el.clientHeight;
    });
    await evidenceReady();
    await metrics('fullscreen-selected'); await shot('05-fullscreen-selected');
    assert.equal(await page.getByRole('button', { name: '退出全屏', exact: true }).isVisible(), true);
    await page.getByRole('button', { name: '退出全屏', exact: true }).click();
    await page.waitForFunction(() => !document.fullscreenElement);
    await page.getByRole('button', { name: '全屏', exact: true }).click();
    await page.waitForFunction(() => !!document.fullscreenElement);
    await page.keyboard.press('Escape');
    // Headless Chromium may not emulate the OS fullscreen Escape shortcut.
    if (await page.evaluate(() => !!document.fullscreenElement)) await page.evaluate(() => document.exitFullscreen());
    await page.waitForFunction(() => !document.fullscreenElement);
    await page.getByRole('button', { name: '全屏', exact: true }).waitFor({state:'visible'});
    await reset();
    await page.getByRole('button', { name: '全屏', exact: true }).click();
    await page.locator('.graph-node-search').fill('机械密封渗漏风险');
    await page.locator('.graph-node-search').press('Enter');
    // Opening details resizes fullscreen: keep the requested neighborhood centered.
    await page.waitForFunction(() => {
      const el = document.querySelector('#graph-canvas'), cy = el._cyreg.cy;
      const node = cy.nodes().filter(n => n.data('label') === '机械密封渗漏风险')[0];
      const b = node.closedNeighborhood().renderedBoundingBox();
      return !document.querySelector('#graph-inspector').hidden &&
        Math.abs((b.x1 + b.x2) / 2 - el.clientWidth / 2) < 1 &&
        Math.abs((b.y1 + b.y2) / 2 - el.clientHeight / 2) < 1;
    });
    await reset();
    await page.locator('.graph-node-search').fill('循环水泵');
    await page.locator('.graph-node-search').press('Enter');
    await page.getByRole('button', { name: '打开实体档案 →', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('#panel-records').hidden);
    assert.equal(await page.evaluate(() => !!document.fullscreenElement), false);
    for (const [width, height, name] of [[1280, 720, 'laptop'], [1920, 1080, 'large-desktop']]) {
      await page.setViewportSize({ width, height }); await page.reload(); await ready();
      const m = await metrics(name);
      assert.ok(m.canvasTop < height * 0.6, name + ' graph starts too low');
      assert.ok(m.minimumNodeFont >= 12, name + ' node text is unreadable');
      await shot('06-' + name);
      await clickGraph('node', '循环水泵');
      const button = page.getByRole('button', { name: '查看详情 ↓', exact: true });
      const box = await button.boundingBox();
      assert.ok(box.y >= 0 && box.y + box.height <= height, 'details action must be in viewport');
      await shot('07-' + name + '-selected');
      await button.click(); await page.waitForFunction(() => document.activeElement?.id === 'inspector-summary');
      await evidenceReady();
      await shot('08-' + name + '-details');
    }
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ base, observations, pageErrors: errors }, null, 2) + '\n');
    console.log(JSON.stringify({ status: 'passed', output, observations }, null, 2));
  } finally { await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
