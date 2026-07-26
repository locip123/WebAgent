'use strict'

const fs = require('fs')
const crypto = require('crypto')
const lunr = require('../lunr.js/lunr.js')

const CHUNK_SIZE = 1500
const CHUNK_OVERLAP = 200
const RESULT_LIMIT = 10
const QUERY_CHARACTER_LIMIT = 512
const QUERY_TOKEN_LIMIT = 64
const DEFAULT_MAX_INDEX_BYTES = 64 * 1024 * 1024

function unique(items) {
  return Array.from(new Set(items.filter(Boolean)))
}

function splitCamelCase(value) {
  return String(value)
    .replace(/(\p{Ll})(\p{Lu})/gu, '$1 $2')
    .replace(/(\p{Lu})(\p{Lu}\p{Ll})/gu, '$1 $2')
}

function lexicalTokens(value) {
  const source = String(value).normalize('NFKC')
  const units = source.match(/[\p{Script=Han}]+|[\p{L}\p{N}]+(?:[._-][\p{L}\p{N}]+)*/gu) || []
  const tokens = []
  for (const originalUnit of units) {
    const unit = originalUnit.toLowerCase()
    if (/^\p{Script=Han}+$/u.test(unit)) {
      const characters = Array.from(unit)
      if (characters.length === 1) tokens.push(unit)
      for (let index = 0; index + 1 < characters.length; index += 1) {
        tokens.push(characters[index] + characters[index + 1])
      }
      continue
    }
    tokens.push(unit)
    const components = originalUnit.split(/[._-]+/u)
    if (components.length > 1) tokens.push(...components.map((component) => component.toLowerCase()))
    for (const component of components) {
      const camelParts = splitCamelCase(component).split(/\s+/u)
      if (camelParts.length > 1) tokens.push(...camelParts.map((part) => part.toLowerCase()))
    }
  }
  const expanded = []
  for (const token of tokens) {
    expanded.push(token)
    if (/^[a-z]+$/u.test(token)) {
      expanded.push(lunr.stemmer(new lunr.Token(token)).toString())
    }
  }
  return unique(expanded)
}

function queryTerms(query) {
  const bounded = Array.from(String(query)).slice(0, QUERY_CHARACTER_LIMIT).join('')
  return {
    bounded,
    truncated: bounded !== String(query),
    terms: lexicalTokens(bounded).slice(0, QUERY_TOKEN_LIMIT),
  }
}

function flattenJson(value, path = '$', keys = [], values = []) {
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      flattenJson(value[index], `${path}[${index}]`, keys, values)
    }
    return { keys, values }
  }
  if (value !== null && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      const childPath = `${path}.${key}`
      keys.push(key, childPath)
      flattenJson(child, childPath, keys, values)
    }
    return { keys, values }
  }
  if (value !== null && value !== undefined) values.push(String(value))
  return { keys, values }
}

function makeJsonChunk(raw, jsonPath, fragment, flattenPath, contextKeys = []) {
  const flattened = flattenJson(fragment, flattenPath)
  return {
    jsonPath,
    raw,
    responseKeys: lexicalTokens([...contextKeys, jsonPath, ...flattened.keys].join(' ')),
    responseValues: lexicalTokens(flattened.values.join(' ')),
  }
}

function jsonChunks(value, path = '$', contextKeys = []) {
  if (value === null || typeof value !== 'object') {
    const raw = JSON.stringify(value)
    if (raw.length <= CHUNK_SIZE) return [makeJsonChunk(raw, path, value, path, contextKeys)]
    return textChunks(raw, path).map((chunk) => ({
      ...chunk,
      responseKeys: lexicalTokens([...contextKeys, path].join(' ')),
    }))
  }

  if (Array.isArray(value)) {
    const chunks = []
    let group = []
    let groupStart = 0
    const flush = () => {
      if (!group.length) return
      const end = groupStart + group.length
      const jsonPath = group.length === 1 ? `${path}[${groupStart}]` : `${path}[${groupStart}:${end}]`
      chunks.push(makeJsonChunk(JSON.stringify(group), jsonPath, group, path, contextKeys))
      group = []
    }
    for (let index = 0; index < value.length; index += 1) {
      const item = value[index]
      const itemRaw = JSON.stringify(item)
      if (itemRaw.length > CHUNK_SIZE) {
        flush()
        chunks.push(...jsonChunks(item, `${path}[${index}]`, contextKeys))
        groupStart = index + 1
        continue
      }
      const candidate = [...group, item]
      if (group.length && JSON.stringify(candidate).length > CHUNK_SIZE) {
        flush()
        groupStart = index
      }
      group.push(item)
    }
    flush()
    return chunks
  }

  const chunks = []
  let group = Object.create(null)
  let groupKeys = []
  const flush = () => {
    if (!groupKeys.length) return
    const jsonPath = groupKeys.length === 1 ? `${path}.${groupKeys[0]}` : path
    chunks.push(makeJsonChunk(JSON.stringify(group), jsonPath, group, path, contextKeys))
    group = Object.create(null)
    groupKeys = []
  }
  for (const [key, child] of Object.entries(value)) {
    const entry = { [key]: child }
    const entryRaw = JSON.stringify(entry)
    if (entryRaw.length > CHUNK_SIZE) {
      flush()
      chunks.push(...jsonChunks(child, `${path}.${key}`, [...contextKeys, key, `${path}.${key}`]))
      continue
    }
    const candidate = { ...group, [key]: child }
    if (groupKeys.length && JSON.stringify(candidate).length > CHUNK_SIZE) flush()
    group[key] = child
    groupKeys.push(key)
  }
  flush()
  return chunks
}

function textChunks(text, jsonPath = null) {
  const chunks = []
  const step = CHUNK_SIZE - CHUNK_OVERLAP
  for (let start = 0; start < text.length; start += step) {
    const raw = text.slice(start, start + CHUNK_SIZE)
    chunks.push({
      jsonPath,
      raw,
      start,
      end: start + raw.length,
      responseKeys: [],
      responseValues: lexicalTokens(raw),
    })
    if (start + CHUNK_SIZE >= text.length) break
  }
  return chunks
}

function responseChunks(request) {
  const body = typeof request.response_body === 'string' ? request.response_body : ''
  if (!body) return []
  try {
    return jsonChunks(JSON.parse(body))
  } catch {
    return textChunks(body)
  }
}

function contentType(request) {
  const headers = request.response_headers
  if (!headers || typeof headers !== 'object') return ''
  const key = Object.keys(headers).find((name) => name.toLowerCase() === 'content-type')
  return key ? String(headers[key]) : ''
}

function requestSummary(request) {
  return {
    timestamp: request.timestamp ?? null,
    url: String(request.url || ''),
    method: String(request.method || ''),
    status: request.status ?? null,
    resource_type: String(request.resource_type || ''),
    post_data: request.post_data ?? null,
    content_type: contentType(request),
    response_truncated: Boolean(request.response_body_truncated),
  }
}

function duplicateKey(request) {
  if (
    request.response_body_truncated
    || typeof request.response_body !== 'string'
  ) return null
  const digest = crypto.createHash('sha256')
  for (const value of [
    request.method || '',
    request.url || '',
    request.post_data || '',
    request.status ?? '',
    request.response_body,
  ]) {
    digest.update(String(value))
    digest.update('\0')
  }
  return digest.digest('hex')
}

function deduplicateRequests(requests) {
  const byKey = new Map()
  const retained = []
  for (const request of requests) {
    const key = duplicateKey(request)
    if (key === null) {
      retained.push({ ...request, duplicate_count: Number(request.duplicate_count || 1) })
      continue
    }
    const existing = byKey.get(key)
    if (!existing) {
      const copy = { ...request, duplicate_count: Number(request.duplicate_count || 1) }
      byKey.set(key, copy)
      retained.push(copy)
      continue
    }
    const duplicateCount = existing.duplicate_count + Number(request.duplicate_count || 1)
    if (Number(request.timestamp || 0) >= Number(existing.timestamp || 0)) {
      const replacement = { ...request, duplicate_count: duplicateCount }
      retained[retained.indexOf(existing)] = replacement
      byKey.set(key, replacement)
    } else {
      existing.duplicate_count = duplicateCount
    }
  }
  return retained
}

function selectWithinIndexBudget(requests, maxIndexBytes) {
  const ordered = [...requests].sort(
    (left, right) => Number(right.timestamp || 0) - Number(left.timestamp || 0)
  )
  const retained = []
  let indexedBytes = 0
  for (const request of ordered) {
    const responseBytes = typeof request.response_body === 'string'
      ? Buffer.byteLength(request.response_body, 'utf8')
      : 0
    if (indexedBytes + responseBytes > maxIndexBytes) continue
    retained.push(request)
    indexedBytes += responseBytes
  }
  return { retained, omitted: requests.length - retained.length }
}

function buildDocuments(requests) {
  const documents = []
  const refs = new Map()
  for (const request of requests) {
    const requestId = Number(request.request_id)
    const metadataRef = `m:${requestId}`
    const metadata = {
      ref: metadataRef,
      url: lexicalTokens(decodeURIComponentSafe(request.url || '')),
      post: lexicalTokens(request.post_data || JSON.stringify(request.json_data || '')),
      response_keys: [],
      response_values: [],
    }
    documents.push(metadata)
    refs.set(metadataRef, { kind: 'metadata', request, requestId })

    responseChunks(request).forEach((chunk, chunkIndex) => {
      const ref = `r:${requestId}:${chunkIndex}`
      documents.push({
        ref,
        url: [],
        post: [],
        response_keys: chunk.responseKeys,
        response_values: chunk.responseValues,
      })
      refs.set(ref, { kind: 'response', request, requestId, chunk, chunkIndex })
    })
  }
  return { documents, refs }
}

function decodeURIComponentSafe(value) {
  try {
    return decodeURIComponent(String(value).replace(/\+/gu, ' '))
  } catch {
    return String(value)
  }
}

function buildIndex(documents) {
  return lunr(function configure() {
    this.ref('ref')
    this.field('url', { boost: 2 })
    this.field('post', { boost: 2 })
    this.field('response_keys', { boost: 2 })
    this.field('response_values', { boost: 2 })
    this.pipeline.reset()
    this.searchPipeline.reset()
    for (const document of documents) this.add(document)
  })
}

function searchIndex(index, terms) {
  if (!terms.length) return []
  return index.query(function query(builder) {
    for (const term of terms) {
      builder.term(term, { boost: 10, usePipeline: false })
      if (/^[a-z]{3,}$/u.test(term)) {
        builder.term(term, {
          boost: 3,
          usePipeline: false,
          wildcard: lunr.Query.wildcard.TRAILING,
        })
      }
      if (/^[a-z]{5,}$/u.test(term)) {
        builder.term(term, { boost: 1, usePipeline: false, editDistance: 1 })
      }
    }
  })
}

function matchedFields(matchData) {
  const fields = new Set()
  for (const fieldMatches of Object.values(matchData.metadata || {})) {
    for (const field of Object.keys(fieldMatches)) fields.add(field)
  }
  return Array.from(fields).sort()
}

function withinOneEdit(left, right) {
  if (left === right) return true
  if (Math.abs(left.length - right.length) > 1) return false
  if (left.length === right.length) {
    const differences = []
    for (let index = 0; index < left.length; index += 1) {
      if (left[index] !== right[index]) differences.push(index)
      if (differences.length > 2) return false
    }
    if (differences.length === 1) return true
    return (
      differences.length === 2
      && differences[1] === differences[0] + 1
      && left[differences[0]] === right[differences[1]]
      && left[differences[1]] === right[differences[0]]
    )
  }
  const [shorter, longer] = left.length < right.length ? [left, right] : [right, left]
  let shortIndex = 0
  let longIndex = 0
  let edits = 0
  while (shortIndex < shorter.length && longIndex < longer.length) {
    if (shorter[shortIndex] === longer[longIndex]) {
      shortIndex += 1
      longIndex += 1
      continue
    }
    edits += 1
    longIndex += 1
    if (edits > 1) return false
  }
  return true
}

function termCoverage(document, terms) {
  const tokens = unique([
    ...document.url,
    ...document.post,
    ...document.response_keys,
    ...document.response_values,
  ])
  return terms.filter((term) => tokens.some((token) => (
    token === term
    || (/^[a-z]{3,}$/u.test(term) && token.startsWith(term))
    || (/^[a-z]{5,}$/u.test(term) && withinOneEdit(term, token))
  )))
}

function chunksOverlap(left, right) {
  if (
    !Number.isFinite(left.start)
    || !Number.isFinite(left.end)
    || !Number.isFinite(right.start)
    || !Number.isFinite(right.end)
  ) return false
  const overlap = Math.max(0, Math.min(left.end, right.end) - Math.max(left.start, right.start))
  const shorter = Math.min(left.end - left.start, right.end - right.start)
  return shorter > 0 && overlap / shorter > 0.5
}

function distinctResponseMatches(matches, limit = 3) {
  const selected = []
  const seenText = new Set()
  for (const match of matches) {
    if (seenText.has(match.chunk.raw)) continue
    if (selected.some((existing) => chunksOverlap(existing.chunk, match.chunk))) continue
    selected.push(match)
    seenText.add(match.chunk.raw)
    if (selected.length === limit) break
  }
  return selected
}

function aggregate(matches, documents, refs, terms) {
  const documentByRef = new Map(documents.map((document) => [document.ref, document]))
  const requests = new Map()
  for (const match of matches) {
    const metadata = refs.get(match.ref)
    if (!metadata) continue
    const document = documentByRef.get(match.ref)
    const matched = termCoverage(document, terms)
    let aggregateRequest = requests.get(metadata.requestId)
    if (!aggregateRequest) {
      aggregateRequest = {
        requestId: metadata.requestId,
        request: metadata.request,
        metadataMatches: [],
        responseMatches: [],
      }
      requests.set(metadata.requestId, aggregateRequest)
    }
    const entry = {
      score: match.score,
      matched,
      matchedFields: matchedFields(match.matchData),
      ...metadata,
    }
    if (metadata.kind === 'response') aggregateRequest.responseMatches.push(entry)
    else aggregateRequest.metadataMatches.push(entry)
  }

  return Array.from(requests.values()).map((item) => {
    item.responseMatches.sort((left, right) => right.score - left.score)
    item.metadataMatches.sort((left, right) => right.score - left.score)
    const best = item.responseMatches[0] || item.metadataMatches[0]
    const responseHit = item.responseMatches.length > 0
    const topChunks = responseHit ? distinctResponseMatches(item.responseMatches) : []
    return {
      request_id: item.requestId,
      score: best.score,
      matched_query_terms: best.matched,
      matched_fields: unique(best.matchedFields),
      duplicate_count: Number(item.request.duplicate_count || 1),
      request: requestSummary(item.request),
      matched_chunks: topChunks.map((match) => ({
        score: match.score,
        text: match.chunk.raw,
        ...(match.chunk.jsonPath ? { json_path: match.chunk.jsonPath } : {}),
      })),
      ...(responseHit || typeof item.request.response_body !== 'string'
        ? {}
        : { response_preview: item.request.response_body.slice(0, CHUNK_SIZE) }),
      _responseHit: responseHit,
      _coverage: best.matched.length,
      _timestamp: Number(item.request.timestamp || 0),
    }
  }).sort((left, right) => (
    Number(right._responseHit) - Number(left._responseHit)
    || right._coverage - left._coverage
    || right.score - left.score
    || right._timestamp - left._timestamp
  )).slice(0, RESULT_LIMIT)
}

function renderResults(items) {
  return items.map((item, index) => {
    const output = { ...item, rank: index + 1 }
    delete output._responseHit
    delete output._coverage
    delete output._timestamp
    return output
  })
}

function search(payload) {
  const capturedRequests = Array.isArray(payload.requests) ? payload.requests : []
  const deduplicatedRequests = deduplicateRequests(capturedRequests)
  const configuredBudget = Number(payload.options && payload.options.max_index_bytes)
  const maxIndexBytes = Number.isFinite(configuredBudget) && configuredBudget >= 0
    ? configuredBudget
    : DEFAULT_MAX_INDEX_BYTES
  const budgeted = selectWithinIndexBudget(deduplicatedRequests, maxIndexBytes)
  const requests = budgeted.retained
  const normalizedQuery = queryTerms(payload.query || '')
  const { documents, refs } = buildDocuments(requests)
  const index = buildIndex(documents)
  const matches = searchIndex(index, normalizedQuery.terms)
  return {
    search_mode: 'lunr',
    query: normalizedQuery.bounded,
    query_truncated: normalizedQuery.truncated || lexicalTokens(normalizedQuery.bounded).length > QUERY_TOKEN_LIMIT,
    indexed_requests: requests.length,
    pending_response_bodies: requests.filter((request) => !request.response_body && request.status == null).length,
    omitted_due_to_budget: budgeted.omitted,
    results: renderResults(aggregate(matches, documents, refs, normalizedQuery.terms)),
  }
}

function main() {
  const source = fs.readFileSync(0, 'utf8')
  const payload = JSON.parse(source)
  process.stdout.write(JSON.stringify(search(payload)))
}

if (require.main === module) main()

module.exports = { search }
