/* 局域网访问令牌助手
 * - 从 URL ?token=、Cookie、localStorage 依次读取令牌
 * - 写入 Cookie，使页面导航（点 logo 返回首页等）自动携带令牌
 * - 全局 fetch 自动附加 X-Access-Token 请求头
 * - paImg(url) 为原图/需鉴权的 img 标签地址附加 ?token=
 */
(function () {
    function getCookie(name) {
        var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : '';
    }

    var urlToken = '';
    try {
        urlToken = new URLSearchParams(window.location.search).get('token') || '';
    } catch (e) {
        urlToken = '';
    }
    if (urlToken) {
        try {
            localStorage.setItem('pa-lan-token', urlToken);
            document.cookie = 'pa_lan_token=' + encodeURIComponent(urlToken) +
                '; path=/; max-age=31536000; SameSite=Lax';
        } catch (e) {}
    }
    var t = urlToken || getCookie('pa_lan_token');
    if (!t) {
        try {
            t = localStorage.getItem('pa-lan-token') || '';
        } catch (e) {
            t = '';
        }
    }
    window.paToken = t;

    var orig = window.fetch ? window.fetch.bind(window) : null;
    if (orig) {
        window.fetch = function (url, opts) {
            opts = opts || {};
            if (t) {
                var headers = new Headers(opts.headers || {});
                headers.set('X-Access-Token', t);
                opts.headers = headers;
            }
            return orig(url, opts);
        };
    }

    window.paImg = function (url) {
        if (t) {
            return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(t);
        }
        return url;
    };
})();