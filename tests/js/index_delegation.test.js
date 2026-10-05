/*
 * index.html 事件委托的单元测试（node tests/js/index_delegation.test.js）。
 *
 * 背景（P0-1）：动态渲染的元素原先把用户可控字符串（作品标题 / 作者名 / 标签名 /
 * 收藏夹名）经 escapeAttr 拼进内联 onclick。escapeAttr 产出的是 HTML 实体，而内联
 * 事件处理器属性会先做实体解码、再编译成 JS —— &#39; 会还原成 '，字符串闭合被击穿，
 * 构成存储型 XSS。修复方式是改用 data-* + 事件委托。
 *
 * 本测试直接把 app/templates/index.html 里那段**真实**的委托代码抽出来，在补了几个
 * 桩函数的 vm 里执行，然后模拟点击，验证每个 data-act 都把参数正确地传给了原函数。
 * 这样以后谁再改回去、或漏掉某条映射，测试会立刻变红。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TEMPLATE = path.join(__dirname, '..', '..', 'app', 'templates', 'index.html');
const html = fs.readFileSync(TEMPLATE, 'utf8');

let failed = 0;
function check(name, ok, detail) {
    if (ok) {
        console.log('[OK] ' + name);
    } else {
        failed += 1;
        console.log('[FAIL] ' + name + (detail ? '   ' + detail : ''));
    }
}
function equal(name, got, want) {
    check(name, got === want, 'got=' + JSON.stringify(got) + ' want=' + JSON.stringify(want));
}

// ---- 模板层面：不得再有「内联 onclick 拼接 escapeAttr」----
const badInline = html.split('\n').filter(l => l.includes('onclick=') && l.includes('escapeAttr('));
equal('模板中不再有内联 onclick 拼接 escapeAttr', badInline.length, 0);

// ---- 抽出真实委托代码并执行 ----
const matched = html.match(/\/\/ ===== 事件委托（data-act）=====([\s\S]*?)\}, true\);/);
check('模板中存在事件委托代码块', !!matched);
const block = matched ? matched[1] + '}, true);' : '';

const calls = [];
const stub = name => (...args) => { calls.push([name, args]); };
let hideCtxCalls = 0;

const listeners = [];
const sandbox = {
    console,
    openFavSelect: stub('openFavSelect'),
    deleteArtworkFromTile: stub('deleteArtworkFromTile'),
    filterByAuthor: stub('filterByAuthor'),
    filterByFavorite: stub('filterByFavorite'),
    deleteFavorite: stub('deleteFavorite'),
    deleteBookmarkSub: stub('deleteBookmarkSub'),
    closeViewerToAuthor: stub('closeViewerToAuthor'),
    closeViewerToTag: stub('closeViewerToTag'),
    closeViewerToFavorite: stub('closeViewerToFavorite'),
    // 单独计数：它也是命中后的正常副作用，但不属于「动作分派」本身
    hideCtxMenu: () => { hideCtxCalls += 1; },
    $: () => null,
    document: {
        addEventListener(type, fn, capture) { listeners.push({ type, fn, capture }); },
    },
};
vm.createContext(sandbox);
vm.runInContext(block, sandbox);

equal('注册了 1 个委托监听', listeners.length, 1);
equal('委托监听类型是 click', listeners[0] && listeners[0].type, 'click');
equal('委托监听在捕获阶段（否则会先触发瓦片自身的打开查看器）',
    listeners[0] && listeners[0].capture, true);

function fire(act, dataset) {
    calls.length = 0;
    const el = { dataset: Object.assign({ act }, dataset) };
    const event = {
        target: { closest: sel => (sel === '[data-act]' ? el : null) },
        defaultPrevented: false,
        propagationStopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.propagationStopped = true; },
    };
    listeners[0].fn(event);
    return event;
}

function expect(name, event, want) {
    equal(name, JSON.stringify(calls), JSON.stringify(want));
}

let e = fire('tile-fav', { id: '42' });
expect('tile-fav → openFavSelect(42)', e, [['openFavSelect', [42]]]);

// 关键用例：标题里带单引号（正是旧写法能被击穿的那种输入）必须原样传下去
e = fire('tile-delete', { id: '7', title: "x', alert(1)//" });
expect('tile-delete 原样传递含单引号的标题', e,
    [['deleteArtworkFromTile', [7, "x', alert(1)//"]]]);
equal('命中后阻止默认行为与冒泡', e.defaultPrevented && e.propagationStopped, true);
check('命中后关闭了右键菜单（传播被截断，需补做）', hideCtxCalls >= 1);

e = fire('filter-author', { name: '作者 A' });
expect('filter-author → filterByAuthor', e, [['filterByAuthor', ['作者 A']]]);

e = fire('filter-fav', { id: '3', name: '夹子' });
expect('filter-fav → filterByFavorite', e, [['filterByFavorite', [3, '夹子']]]);

e = fire('fav-delete', { id: '3', name: '夹子' });
expect('fav-delete → deleteFavorite', e, [['deleteFavorite', [3, '夹子']]]);

e = fire('sub-delete', { uid: '99', name: '' });
expect('sub-delete → deleteBookmarkSub', e, [['deleteBookmarkSub', [99, '']]]);

e = fire('viewer-author', { id: '5', name: 'N' });
expect('viewer-author → closeViewerToAuthor(name, id)', e, [['closeViewerToAuthor', ['N', 5]]]);

e = fire('viewer-tag', { name: 'R-18' });
expect('viewer-tag → closeViewerToTag', e, [['closeViewerToTag', ['R-18']]]);

e = fire('viewer-fav', { id: '2', name: 'F' });
expect('viewer-fav → closeViewerToFavorite', e, [['closeViewerToFavorite', [2, 'F']]]);

// 未登记的 data-act 与普通点击都不应触发任何动作
calls.length = 0;
listeners[0].fn({
    target: { closest: () => ({ dataset: { act: 'not-registered' } }) },
    preventDefault() {}, stopPropagation() {},
});
equal('未登记的 data-act 不触发任何动作', calls.length, 0);

calls.length = 0;
listeners[0].fn({
    target: { closest: () => null },
    preventDefault() {}, stopPropagation() {},
});
equal('非 data-act 目标的点击不触发任何动作', calls.length, 0);

// ---- 模板层面：9 条映射都还在 ----
for (const act of ['tile-fav', 'tile-delete', 'filter-author', 'filter-fav', 'fav-delete',
                   'sub-delete', 'viewer-author', 'viewer-tag', 'viewer-fav']) {
    check('模板中存在 data-act="' + act + '"', html.includes('data-act="' + act + '"'));
}

console.log(failed ? 'FAILED=' + failed : 'ALL_PASSED');
process.exit(failed ? 1 : 0);
