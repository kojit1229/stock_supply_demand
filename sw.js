'use strict';

const CACHE_NAME = 'jukyu-v5';
const APP_SHELL = ['./', './index.html', './manifest.json'];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function (cache) { return cache.addAll(APP_SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (names) {
        return Promise.all(names.map(function (name) {
          return name.indexOf('jukyu-') === 0 && name !== CACHE_NAME ? caches.delete(name) : Promise.resolve(false);
        }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

function isDataJson(request) {
  const url = new URL(request.url);
  return url.origin === self.location.origin && /\/data\/.*\.json$/.test(url.pathname);
}

function networkFirst(request) {
  return fetch(request)
    .then(function (response) {
      if (response && response.ok) {
        return caches.open(CACHE_NAME)
          .then(function (cache) { return cache.put(request, response.clone()); })
          .then(function () { return response; });
      }
      return caches.match(request).then(function (cached) { return cached || response; });
    })
    .catch(function () {
      return caches.match(request).then(function (cached) {
        if (cached) return cached;
        return new Response(JSON.stringify({ error: 'offline' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json; charset=utf-8' }
        });
      });
    });
}

function cacheFirst(request) {
  return caches.match(request).then(function (cached) {
    if (cached) return cached;
    return fetch(request).then(function (response) {
      if (response && response.ok && request.method === 'GET') {
        return caches.open(CACHE_NAME)
          .then(function (cache) { return cache.put(request, response.clone()); })
          .then(function () { return response; });
      }
      return response;
    });
  });
}

self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') return;
  event.respondWith(isDataJson(event.request) ? networkFirst(event.request) : cacheFirst(event.request));
});
