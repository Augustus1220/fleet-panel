const formatTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Singapore', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
})
const svgNamespace = 'http://www.w3.org/2000/svg'
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)')
const animationOptions = {duration: 430, easing: 'cubic-bezier(.22, .61, .36, 1)'}
const counts = new WeakMap()
function formatBytes(bytes, reference = bytes) {
  if (bytes === null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let index = 0
  while (reference >= 1024 && index < units.length - 1) {
    reference /= 1024
    bytes /= 1024
    index += 1
  }
  return Number(bytes.toFixed(index ? 1 : 0)) + ' ' + units[index]
}

function formatPercent(value) {
  return Number(value.toFixed(1)) + '%'
}

function formatShare(value) {
  return value > 0 && value < 0.1 ? '<0.1%' : formatPercent(value)
}

function loadColor(value) {
  if (value <= 60) return 'var(--green)'
  if (value < 75) return 'color-mix(in srgb, var(--green), var(--amber) ' + (value - 60) / 15 * 100 + '%)'
  if (value < 90) return 'color-mix(in srgb, var(--amber), var(--red) ' + (value - 75) / 15 * 100 + '%)'
  return 'var(--red)'
}

function animateNumber(element, target, format, initial = 0) {
  const previous = counts.get(element)
  if (previous?.target === target && !reducedMotion.matches) return
  if (previous) cancelAnimationFrame(previous.frame)
  const start = previous?.value ?? initial
  const state = {target, value: start, frame: 0}
  counts.set(element, state)
  const finish = () => {
    state.value = target
    element.textContent = target === null ? '—' : format(target)
  }
  if (target === null || start === target || reducedMotion.matches) return finish()
  const startTime = performance.now()
  const tick = now => {
    const progress = Math.max(0, Math.min(1, (now - startTime) / animationOptions.duration))
    if (progress === 1 || reducedMotion.matches) return finish()
    state.value = start + (target - start) * (1 - (1 - progress) ** 3)
    element.textContent = format(state.value, target || start)
    state.frame = requestAnimationFrame(tick)
  }
  element.textContent = format(start, target || start)
  state.frame = requestAnimationFrame(tick)
}

function animateHostValues(host, previous = new Map()) {
  for (const element of host.querySelectorAll('[data-motion-key]')) {
    const {motionKey, motionType, motionValue} = element.dataset
    const value = Number(motionValue)
    const from = previous.get(motionKey) ?? 0
    if (from === value || reducedMotion.matches) continue
    if (motionType === 'meter') {
      element.animate({transform: ['scaleX(' + from + ')', 'scaleX(' + value + ')'],
        backgroundColor: [loadColor(from * 100), loadColor(value * 100)]}, animationOptions)
    } else if (motionType === 'ring') {
      element.animate({'--used': [from + '%', value + '%'],
        '--load-color': [loadColor(from), loadColor(value)]}, animationOptions)
    } else {
      const formats = {bytes: formatBytes, percent: formatPercent, share: formatShare}
      if (motionType === 'percent') {
        element.animate({color: [loadColor(from), loadColor(value)]}, animationOptions)
      }
      animateNumber(element, value, formats[motionType], from)
    }
  }
}

function svgElement(tag, attributes) {
  const element = document.createElementNS(svgNamespace, tag)
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value)
  return element
}

function formatBucket(bucket) {
  return formatTime.format(bucket.start * 1000)
}

const trafficRequests = new Map()
const CACHE_TIME = 30000

function trafficUrl(node, period) {
  return '/api/v1/nodes/' + encodeURIComponent(node) + '/traffic?range=' + period
}

function requestTraffic(url, force = false) {
  const existing = trafficRequests.get(url)
  if (existing && (!existing.loaded || (!force && Date.now() - existing.loaded < CACHE_TIME))) {
    return existing.request
  }

  const entry = {loaded: 0}
  entry.request = fetch(url, {cache: 'no-store'}).then(async response => {
    if (!response.ok) throw Object.assign(new Error('读取流量失败'), {status: response.status})
    const data = await response.json()
    entry.loaded = Date.now()
    return data
  }).catch(error => {
    if (trafficRequests.get(url) === entry) trafficRequests.delete(url)
    throw error
  })
  trafficRequests.delete(url)
  trafficRequests.set(url, entry)
  if (trafficRequests.size > 8) trafficRequests.delete(trafficRequests.keys().next().value)
  return entry.request
}

export function preloadTraffic(workspace) {
  const period = workspace.querySelector('#period')
  const panels = workspace.querySelectorAll('.traffic-pane[data-node]')
  if (!period || !panels.length) return null
  return Promise.all(Array.from(panels, panel =>
    requestTraffic(trafficUrl(panel.dataset.node, period.value))))
}

let panelSequence = 0

function createTrafficPanel(panel, period) {
  const nodeId = panel.dataset.node
  const account = panel.querySelector('.account')
  const chart = panel.querySelector('.traffic-chart')
  const chartScale = panel.querySelector('.chart-scale')
  const marker = panel.querySelector('.chart-marker')
  const total = panel.querySelector('.traffic-total')
  const reading = panel.querySelector('.chart-reading')
  const chartStart = panel.querySelector('.chart-start')
  const chartEnd = panel.querySelector('.chart-end')
  const table = panel.querySelector('.traffic-rows')
  const clipId = 'traffic-reveal-' + (++panelSequence)
  let query = 0
  let result = null
  let buckets = []
  let selectedPoint = null
  let scale = 1
  let plotVersion = ''

  function addRow(label, value) {
    const row = table.insertRow()
    row.insertCell().textContent = label
    row.insertCell().textContent = value
  }

  function xAt(index) {
    return 600 * index / Math.max(1, buckets.length - 1)
  }

  function yAt(bytes) {
    return 132 - 124 * bytes / scale
  }

  function selectPoint(index) {
    const bucket = buckets[index]
    if (!bucket || !marker) return
    selectedPoint = index
    marker.style.left = xAt(index) / 6 + '%'
    marker.style.top = 100 * yAt(bucket.bytes ?? 0) / 140 + '%'
    marker.hidden = bucket.bytes === null
    reading.textContent = formatBucket(bucket) + ' · ' + (bucket.bytes === null ? '缺少采样' : formatBytes(bucket.bytes))
  }

  function drawChart(hasData) {
    scale = Math.max(4, ...buckets.map(bucket => bucket.bytes ?? 0))
    const ticks = []
    const grid = []
    for (let i = 0; i <= 4; i++) {
      const value = scale * (1 - i / 4)
      const y = yAt(value)
      grid.push('M0,' + y + ' H600')
      if (hasData) {
        const tick = document.createElement('span')
        tick.style.setProperty('--tick', (100 * y / 140) + '%')
        tick.textContent = formatBytes(value)
        ticks.push(tick)
      }
    }
    chartScale.replaceChildren(...ticks)
    let continued = false
    let points = ''
    buckets.forEach((bucket, index) => {
      if (bucket.bytes === null) {
        continued = false
        return
      }
      const x = xAt(index)
      const y = yAt(bucket.bytes)
      points += continued ? ' L' + x + ',' + y : ' M' + x + ',' + y + ' h' + (x === 600 ? -0.5 : 0.5)
      continued = true
    })
    const gridLines = svgElement('path', {d: grid.join(' '), class: 'chart-grid', fill: 'none'})
    const path = svgElement('path', {d: points, class: 'traffic-line', stroke: 'currentColor', fill: 'none',
      'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke'})
    chart.replaceChildren(gridLines, path)
    marker.hidden = true
    if (hasData && !reducedMotion.matches) {
      const clip = svgElement('clipPath', {id: clipId})
      const reveal = svgElement('rect', {width: 600, height: 140})
      clip.append(reveal)
      chart.prepend(clip)
      path.setAttribute('clip-path', 'url(#' + clipId + ')')
      const first = buckets.findIndex(bucket => bucket.bytes !== null)
      reveal.animate({transform: ['scaleX(' + xAt(first) / 600 + ')', 'scaleX(1)']}, animationOptions)
    }
  }

  function showTraffic() {
    if (!result) return
    const values = result.accounts[account.value]
    buckets = account.value && values
      ? result.buckets.map((bucket, index) => ({start: bucket.start, bytes: values[index]}))
      : result.buckets
    const known = buckets.filter(bucket => bucket.bytes !== null)
    const nextVersion = JSON.stringify(buckets)
    if (nextVersion !== plotVersion) {
      drawChart(known.length > 0)
      plotVersion = nextVersion
    }
    animateNumber(total, known.length ? known.reduce((sum, bucket) => sum + bucket.bytes, 0) : null, formatBytes)
    chartStart.textContent = buckets.length ? formatBucket(buckets[0]) : ''
    chartEnd.textContent = buckets.length ? formatBucket(buckets[buckets.length - 1]) : ''
    reading.textContent = known.length ? '' : '这个周期暂无可用采样'
    if (known.length && selectedPoint !== null) selectPoint(Math.min(selectedPoint, buckets.length - 1))
    table.replaceChildren()
    for (const bucket of buckets.slice().reverse()) addRow(formatBucket(bucket), formatBytes(bucket.bytes))
  }

  async function load(force = false) {
    const current = ++query
    try {
      const next = await requestTraffic(trafficUrl(nodeId, period.value), force)
      if (current !== query) return
      result = next
      const selected = account.value
      const names = Object.keys(result.accounts).sort()
      if (names.join('\0') !== Array.from(account.options).slice(1).map(option => option.value).join('\0')) {
        account.replaceChildren(new Option('全部账号', ''), ...names.map(name => new Option(name, name)))
        if (names.includes(selected)) account.value = selected
      }
      showTraffic()
    } catch (error) {
      if (current !== query) return
      if (error.status === 401) return location.assign('/')
      result = null
      buckets = []
      plotVersion = ''
      chart.replaceChildren()
      chartScale.replaceChildren()
      marker.hidden = true
      table.replaceChildren()
      animateNumber(total, null, formatBytes)
      chartStart.textContent = chartEnd.textContent = ''
      reading.textContent = '暂时无法读取流量'
    }
  }

  function selectFromPointer(event) {
    if (!buckets.length) return
    const bounds = chart.getBoundingClientRect()
    const fraction = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width))
    selectPoint(Math.round(fraction * (buckets.length - 1)))
  }
  chart.addEventListener('pointermove', event => {
    if (event.pointerType !== 'touch') selectFromPointer(event)
  })
  chart.addEventListener('click', event => {
    if (event.detail !== 0) selectFromPointer(event)
  })
  chart.addEventListener('keydown', event => {
    if (!buckets.length || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return
    event.preventDefault()
    const step = event.key === 'ArrowLeft' ? -1 : 1
    selectPoint(Math.max(0, Math.min(buckets.length - 1, (selectedPoint ?? 0) + step)))
  })
  account.addEventListener('change', showTraffic)
  return {load}
}

export function bindWorkspace(workspace, viewUrl) {
  const period = workspace.querySelector('#period')
  const trafficPanels = period
    ? Array.from(workspace.querySelectorAll('.traffic-pane[data-node]'),
        panel => createTrafficPanel(panel, period))
    : []
  const refreshButton = workspace.querySelector('#refresh-panel')
  const refreshStatus = workspace.querySelector('#refresh-status')
  const hostFeed = workspace.querySelector('#host-feed')
  const eventFeed = workspace.querySelector('#event-feed')

  if (!refreshButton) return null

  function updateFragment(host, replacement) {
    if (!replacement || host.contains(document.activeElement)) return
    const open = new Set(Array.from(host.querySelectorAll('details[open]'), item => item.dataset.panel))
    const eventScroll = host.querySelector('.events')?.scrollTop ?? 0
    const previous = new Map(Array.from(host.querySelectorAll('[data-motion-key]'),
      element => [element.dataset.motionKey, Number(element.dataset.motionValue)]))
    replacement.querySelectorAll('details').forEach(item => {
      item.open = open.has(item.dataset.panel)
    })
    host.replaceChildren(...replacement.childNodes)
    host.querySelector('.events')?.scrollTo(0, eventScroll)
    if (host === hostFeed) animateHostValues(host, previous)
  }

  async function refresh() {
    if (document.hidden || refreshButton.disabled) return
    refreshButton.disabled = true
    refreshStatus.textContent = ''
    try {
      if (trafficPanels.length) {
        await Promise.all(trafficPanels.map(panel => panel.load(true)))
        return
      }
      const panel = eventFeed ? 'events' : 'hosts'
      const params = new URLSearchParams(eventFeed ? viewUrl.search : '')
      params.set('panel', panel)
      const response = await fetch('/feed?' + params)
      if (response.status === 401) return location.assign('/')
      if (!response.ok) throw Error()
      const page = new DOMParser().parseFromString(await response.text(), 'text/html')
      updateFragment(eventFeed || hostFeed, page.querySelector('[data-fragment="' + panel + '"]'))
    } catch {
      refreshStatus.textContent = '刷新失败'
    } finally {
      refreshButton.disabled = false
    }
  }

  if (trafficPanels.length) {
    period.addEventListener('change', () => trafficPanels.forEach(panel => panel.load()))
    trafficPanels.forEach(panel => panel.load())
  }
  if (hostFeed) animateHostValues(hostFeed)
  refreshButton.addEventListener('click', refresh)
  return refresh
}
