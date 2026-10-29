const fs = require('node:fs');
const assert = require('node:assert/strict');
const css = fs.readFileSync('app/static/styles.css', 'utf8');
function luminance(hex) {
  const rgb = hex.replace('#','').match(/../g).map(v => parseInt(v,16)/255)
    .map(v => v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4);
  return .2126*rgb[0]+.7152*rgb[1]+.0722*rgb[2];
}
function ratio(a,b) {
  const values = [luminance(a), luminance(b)].sort((a,b)=>b-a);
  return (values[0]+.05)/(values[1]+.05);
}
for (const [dark,background] of [[false,'#ffffff'],[true,'#1e293b']]) {
  for (const tone of ['is-lent','is-owed']) {
    const selector = `${dark ? 'html.theme-dark ' : ''}.split-outcome.${tone}`;
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    const match = css.match(new RegExp(`(?:^|\\n)${escaped}\\s*\\{[^}]*color:\\s*(#[a-fA-F0-9]{6})`));
    assert.ok(match, selector);
    assert.ok(ratio(match[1],background) >= 4.5, `${selector}: insufficient contrast`);
  }
}
console.log('4 light/dark financial outcome contrast checks passed');
