import {bindWorkspace, preloadTraffic} from './dashboard.js'

const nav = document.querySelector('.primary-nav')
const tabs = Array.from(nav.querySelectorAll('a'))
const paths = new Set(tabs.map(tab => new URL(tab.href).pathname))
const cache = new Map()
const requests = new Map()
const recent = new Map()
const cacheLimit = 8
const refreshEvery = 30000
let navigation = 0

function viewKey(url) {
  return url.pathname + url.search
}

function remember(view) {
  if (view.workspace.dataset.cacheable !== 'true') return
  const key = viewKey(view.url)
  cache.delete(key)
  cache.set(key, view)
  const pinned = new Set(Array.from(recent.values(), viewKey))
  pinned.add(key)
  for (const candidate of cache.keys()) {
    if (cache.size <= cacheLimit) break
    if (!pinned.has(candidate)) cache.delete(candidate)
  }
}

function loadView(url) {
  const key = viewKey(url)
  if (cache.has(key)) return Promise.resolve(cache.get(key))
  if (requests.has(key)) return requests.get(key)
  const request = fetch(key, {headers: {'X-Fleet-View': 'workspace'}, cache: 'no-store'}).then(async response => {
    if (!response.ok || response.redirected) throw Error('读取模块失败')
    const page = new DOMParser().parseFromString(await response.text(), 'text/html')
    const workspace = page.querySelector('.workspace[data-cacheable="true"]')
    if (!workspace) throw Error('模块不可用')
    const view = {url, workspace, loaded: Date.now()}
    remember(view)
    return view
  }).finally(() => requests.delete(key))
  requests.set(key, request)
  return request
}

function initialize(view) {
  if (view.refresh === undefined) view.refresh = bindWorkspace(view.workspace, view.url)
}

function refresh(view) {
  if (view.refresh) {
    view.loaded = Date.now()
    view.refresh()
  }
}

const initial = document.querySelector('.workspace')
if (initial.dataset.cacheable === 'false') {
  // POST responses can show a one-time enrollment token. Back returns to the clean GET route.
  const currentTab = nav.querySelector('[aria-current="page"]')
  if (currentTab) history.replaceState(history.state, '', currentTab.href)
}
let active = {url: new URL(location.href), workspace: initial, loaded: Date.now()}
initialize(active)
remember(active)
recent.set(active.url.pathname, active.url)

async function navigate(url, push = true) {
  const attempt = ++navigation
  const key = viewKey(url)
  if (key === viewKey(active.url) && active.workspace.dataset.cacheable === 'true') {
    nav.removeAttribute('aria-busy')
    return
  }
  const cached = cache.has(key)
  if (!cached) nav.setAttribute('aria-busy', 'true')
  try {
    const next = await loadView(url)
    if (attempt !== navigation) return
    active.scroll = Array.from(active.workspace.querySelectorAll('.module-body, .events, .table'),
      element => [element, element.scrollLeft, element.scrollTop])
    next.workspace.classList.toggle('is-returning', cached)
    active.workspace.replaceWith(next.workspace)
    active = next
    remember(next)
    recent.set(url.pathname, url)
    if (push) history.pushState(null, '', url)
    for (const tab of tabs) {
      if (new URL(tab.href).pathname === url.pathname) tab.setAttribute('aria-current', 'page')
      else tab.removeAttribute('aria-current')
    }
    document.title = next.workspace.dataset.title
    for (const [element, x, y] of next.scroll || []) element.scrollTo(x, y)
    const heading = next.workspace.querySelector('#module-title')
    heading.tabIndex = -1
    heading.focus({preventScroll: true})
    initialize(next)
    if (Date.now() - next.loaded >= refreshEvery) refresh(next)
  } catch {
    if (attempt === navigation) location.assign(url)
  } finally {
    if (attempt === navigation) nav.removeAttribute('aria-busy')
  }
}

document.addEventListener('click', event => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  const link = event.target.closest('.primary-nav a, .topbar .brand, #event-feed a')
  if (!link || link.target || link.hasAttribute('download')) return
  let url = new URL(link.href)
  if (url.origin !== location.origin || !paths.has(url.pathname)) return
  if (nav.contains(link)) url = recent.get(url.pathname) ?? url
  event.preventDefault()
  navigate(url)
})

document.addEventListener('submit', event => {
  const form = event.target
  if (!form.matches('.event-filters') || event.defaultPrevented) return
  const url = new URL(form.action)
  url.search = new URLSearchParams(new FormData(form))
  event.preventDefault()
  navigate(url)
})

window.addEventListener('popstate', () => navigate(new URL(location.href), false))

const preloadOthers = async () => {
  if (document.hidden || navigator.connection?.saveData) return
  let trafficView
  for (const tab of tabs) {
    const url = new URL(tab.href)
    if (url.pathname === active.url.pathname) continue
    const view = await loadView(url).catch(() => null)
    if (view?.workspace.querySelector('.traffic-chart')) trafficView = view
  }
  if (trafficView) await preloadTraffic(trafficView.workspace)?.catch(() => {})
}

if (window.requestIdleCallback) requestIdleCallback(preloadOthers, {timeout: 1200})
else setTimeout(preloadOthers, 300)
setInterval(() => {
  if (!document.hidden) refresh(active)
}, refreshEvery)
