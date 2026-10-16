/*
 * Redacted stand-in for Spotify's web-player ENTRY bundle, used by
 * tests/test_spotify_gql_hash_discovery.py. Only the two shapes the parser must
 * survive are reproduced, with synthetic operation names and synthetic hashes
 * (no real bundle, no real hash — a hash is a public constant, but a fixture
 * that copies a live bundle rots and tells us nothing):
 *
 *   1. the persisted-query triple   new <x>.<l>("<op>", "query"|,"mutation", "<64 hex>")
 *      — emitted both as `new i.l(` and as `new (a(2700)).l(`, and nothing else
 *      about the minified constructor may be load-bearing for the parser;
 *   2. the webpack chunk-URL builder `.u=e=>""+({ids:names})[e]+"."+({ids:hash})[e]+".js"`
 *      — including an id that has a hash but no name (webpack falls back to the
 *      numeric id as the file name).
 */
var u = {p: "https://open.spotifycdn.com/cdn/build/web-player/"};
13180(e, t, r) {
  "use strict";
  var i = r(2700);
  let n = new i.l("queryWhatsNewFeed", "query", "1111111111111111111111111111111111111111111111111111111111111111", null),
      a = new i.l("whatsNewFeedNewItems", "query", "2222222222222222222222222222222222222222222222222222222222222222", null);
},
61615(e, t, a) {
  // constructor written as a call — the shape that made a `new X.l(`-only
  // regex miss a fifth of the operations in a real bundle
  let s = new (a(2700)).l("addFakeToLibrary", "mutation", "3333333333333333333333333333333333333333333333333333333333333333", null);
},
77777(e, t, r) {
  // decoy: a 64-hex content hash with no operation in front of it must NOT
  // become a persisted query
  let img = {src: "4444444444444444444444444444444444444444444444444444444444444444"};
},
u.u = e => "" + ({
  1328: "xpui-pip-mini-player",
  1401: "xpui-routes-artist"
})[e] + "." + ({
  1049: "9b83acfa",
  1328: "c6340921",
  1401: "44e273a5"
})[e] + ".js",
u.miniCssF = e => "" + e + ".css";
