'use strict';
// Shared header and button bar for the game's Store integration.
const { withAlpha } = require('./canvas');
const { t: localizeLabel } = require('./i18n');
const upperText = (value) => localizeLabel(String(value)).toUpperCase();

function tabWidths(cv, names, size, opt, min, pad, room, pad0) {
    const tw = names.map((n) => cv.textWidth(n, size, opt));
    const wide = tw.map((w) => Math.max(min, w + pad));
    if (wide.reduce((a, b) => a + b, 0) <= room) return wide;
    const p = Math.max(pad0, Math.min(pad, (room - tw.reduce((a, b) => a + b, 0)) / Math.max(1, names.length)));
    return tw.map((w) => w + p);
}
/** Badges right-aligned at xr (right edge), top y, height h, text size. Returns the left edge. */
function badgeRow(cv, t, xr, y, h, size, badges) {
    const opt = { align: 'center', font: 'bold', spacing: 1.5 };
    let bx = xr;
    badges.forEach((b) => {
        const bw = Math.max(h * 3.5, cv.textWidth(b.text, size, opt) + h), c = t[b.color] || b.color || t.warn;
        cv.roundRect(bx - bw, y, bw, h, h * 0.2, withAlpha(c, 0.2));
        cv.text(b.text, bx - bw / 2, y + h / 2 + size * 0.36, size, c, opt);
        bx -= bw + h * 0.4;
    });
    return bx;
}

/**
 * Full-screen header in screen pixels (x, y, w, h): the app's name, its page tabs (selected
 * one lit, with an underline) and badges on the right. Tabs that don't fit drop the name.
 * flat: no sheen (plain t.bg).
 * compact: smaller text, badges right after the tabs and only as wide as that content, so the
 * HMI's own status strip (radio station, phone signal, icons) can show to the right of it.
 * Returns the x where the header's content ends.
 */
function header(cv, t, x, y, w, h, title, tabs, sel, badges, flat, compact) {
    if (compact) return headerCompact(cv, t, x, y, w, h, title, tabs, sel, badges);
    const S = t.sel || t.accent, opt = { align: 'center', font: 'bold', spacing: 2 };
    cv.fillRect(x, y, w, h, t.bg);
    // flat: plain, to match the HMI's own black strip next to it
    if (!flat) cv.gradientRect(x, y, w, h, [[0, withAlpha('#ffffff', 0.02)], [1, withAlpha('#ffffff', 0.08)]]);
    cv.fillRect(x, y + h - 2, w, 2, withAlpha('#ffffff', 0.22));
    const right = badgeRow(cv, t, x + w - 16, y + (h - 30) / 2, 30, 18, badges || []);
    const names = (tabs || []).map((n) => upperText(n));
    const tw = tabWidths(cv, names, 20, opt, 170, 64, right - x - 20, 24);
    const tabsW = tw.reduce((a, b) => a + b, 0);
    let tx = x + 20;
    const tsize = 38, titleW = title ? cv.textWidth(title, tsize) : 0;
    if (title && tx + titleW + 40 + tabsW <= right) {
        cv.text(title, tx, y + h / 2 + tsize * 0.36, tsize, t.text);
        tx += titleW + 40;
    }
    cv.tabRects = [];
    names.forEach((n, i) => {
        const on = i === sel, bw = tw[i];
        if (tx + bw > right) return;
        cv.tabRects.push({ i, x: tx, y, w: bw, h });
        if (on) {
            cv.gradientRect(tx, y, bw, h, [[0, withAlpha(S, 0)], [1, withAlpha(S, 0.25)]]);
            cv.fillRect(tx, y + h - 5, bw, 5, S);
        }
        cv.text(n, tx + bw / 2, y + h / 2 + 7, 20, on ? t.selText || '#ffffff' : t.dim, opt);
        if (i < names.length - 1) cv.fillRect(tx + bw, y + h * 0.28, 1, h * 0.44, withAlpha('#ffffff', 0.14));
        tx += bw;
    });
}

function headerCompact(cv, t, x, y, w, h, title, tabs, sel, badges) {
    const S = t.sel || t.accent, opt = { align: 'center', font: 'bold', spacing: 1.6 };
    const bopt = { align: 'center', font: 'bold', spacing: 1.5 };
    const names = (tabs || []).map((n) => upperText(n));
    const bh = 30, bsize = 17;
    const bws = (badges || []).map((b) => Math.max(bh * 3.5, cv.textWidth(b.text, bsize, bopt) + bh));
    const badgesW = bws.reduce((a, b) => a + b + bh * 0.4, 0);
    const tsize = 32, titleW = title ? cv.textWidth(title, tsize) : 0;
    const room = w - 20 - (badgesW ? badgesW + 12 : 0);
    const tw = tabWidths(cv, names, 18, opt, 132, 44, room - 20, 20);
    const tabsW = tw.reduce((a, b) => a + b, 0);
    // the tabs start 20 px in: the name shows only where every tab still fits after it (else the last tab, Settings, drops)
    const showTitle = title && 20 + titleW + 32 + tabsW <= room;
    // only as wide as the content: the rest of the strip stays the HMI's
    let end = x + 20 + (showTitle ? titleW + 32 : 0);
    names.forEach((n, i) => { if (end + tw[i] <= x + room) end += tw[i]; });
    end = Math.min(x + w, end + (badgesW ? 12 + badgesW : 0) + 16);
    cv.fillRect(x, y, end - x, h, t.bg);
    cv.fillRect(x, y + h - 2, end - x, 2, withAlpha('#ffffff', 0.22));
    let tx = x + 20;
    if (showTitle) {
        cv.text(title, tx, y + h / 2 + tsize * 0.36, tsize, t.text);
        tx += titleW + 32;
    }
    cv.tabRects = [];
    names.forEach((n, i) => {
        const on = i === sel, bw = tw[i];
        if (tx + bw > x + room) return;
        cv.tabRects.push({ i, x: tx, y, w: bw, h });
        if (on) {
            cv.gradientRect(tx, y, bw, h, [[0, withAlpha(S, 0)], [1, withAlpha(S, 0.25)]]);
            cv.fillRect(tx, y + h - 4, bw, 4, S);
        }
        cv.text(n, tx + bw / 2, y + h / 2 + 6, 18, on ? t.selText || '#ffffff' : t.dim, opt);
        if (i < names.length - 1) cv.fillRect(tx + bw, y + h * 0.3, 1, h * 0.4, withAlpha('#ffffff', 0.14));
        tx += bw;
    });
    if (badgesW) badgeRow(cv, t, tx + 12 + badgesW, y + (h - bh) / 2, bh, bsize, badges);
    return end;
}

/**
 * Button bar: buttons = [{ label, x, w }] (x, w of each button), focus = index of the button
 * the iDrive controller is on (-1: none), flash = index of a just-pressed one.
 * variant: 'slim' (the full-screen layout's bar, any height, e.g. 48) or 'standard' (120).
 */
function menuBar(cv, t, x, y, w, h, buttons, focus, variant, flash) {
    const std = variant === 'standard', u = h / (std ? 120 : 48);
    const sel = t.sel || t.accent;
    cv.fillRect(x, y, w, h, t.bg);
    cv.gradientRect(x, y, w, h, [[0, withAlpha('#ffffff', 0.09)], [1, withAlpha('#ffffff', 0.03)]]);
    cv.fillRect(x, y, w, Math.max(1, 2 * u), withAlpha('#ffffff', 0.2));
    const size = (std ? 30 : 19) * u;
    buttons.forEach((b, i) => {
        const on = i === focus, hit = i === flash;
        if (on || hit) {
            cv.gradientRect(b.x, y, b.w, h, [[0, withAlpha(sel, hit ? 0.5 : 0.08)], [1, withAlpha(sel, hit ? 0.7 : 0.4)]]);
            cv.fillRect(b.x, y + h - 4 * u, b.w, 4 * u, sel);
        }
        const label = std ? String(b.label) : upperText(b.label);
        cv.text(label, b.x + b.w / 2, y + h / 2 + size * 0.36, size, on || hit ? t.selText || '#ffffff' : std ? t.text : t.dim,
            { align: 'center', font: std ? 'label' : 'bold', spacing: std ? 0 : 2 * u });
        if (i < buttons.length - 1) cv.fillRect(b.x + b.w, y + h * 0.25, Math.max(1, u), h * 0.5, withAlpha('#ffffff', 0.13));
    });
}

module.exports = { header, menuBar };
