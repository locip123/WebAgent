'use strict'

const lunr = require('../lunr.js/lunr.js')

const CHUNK_SIZE = 2_000
const CHUNK_OVERLAP = 300
const RESULT_LIMIT = 20
const MAX_EXACT_MATCHES_PER_DOCUMENT = 8
const QUERY_CHARACTER_LIMIT = 512
const QUERY_TOKEN_LIMIT = 64

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
  const original = String(query)
  const bounded = Array.from(original).slice(0, QUERY_CHARACTER_LIMIT).join('')
  return {
    bounded,
    truncated: bounded !== original,
    terms: lexicalTokens(bounded).slice(0, QUERY_TOKEN_LIMIT),
  }
}

function splitChunks(document) {
  const text = String(document.text || '')
  const step = CHUNK_SIZE - CHUNK_OVERLAP
  if (!text) return [{
    ref: `${document.id}:0`,
    source_id: String(document.id),
    source: String(document.source || 'page'),
    field: String(document.field || 'text'),
    filename: String(document.filename || ''),
    url: String(document.url || ''),
    text: '',
    start: 0,
    end: 0,
  }]
  const chunks = []
  for (let start = 0; start < text.length; start += step) {
    const raw = text.slice(start, start + CHUNK_SIZE)
    chunks.push({
      ref: `${document.id}:${chunks.length}`,
      source_id: String(document.id),
      source: String(document.source || 'page'),
      field: String(document.field || 'text'),
      filename: String(document.filename || ''),
      url: String(document.url || ''),
      text: raw,
      start,
      end: start + raw.length,
    })
    if (start + CHUNK_SIZE >= text.length) break
  }
  return chunks
}

function buildIndex(chunks) {
  return lunr(function configure() {
    this.ref('ref')
    this.field('text', { boost: 3 })
    this.field('filename', { boost: 2 })
    this.field('url', { boost: 2 })
    this.field('title', { boost: 1 })
    this.pipeline.reset()
    this.searchPipeline.reset()
    for (const chunk of chunks) {
      this.add({
        ref: chunk.ref,
        text: lexicalTokens(chunk.text),
        filename: lexicalTokens(chunk.filename),
        url: lexicalTokens(chunk.url),
        title: lexicalTokens(chunk.title || ''),
      })
    }
  })
}

function searchIndex(index, terms) {
  if (!terms.length) return []
  const numericTerms = terms.filter((term) => /^\d+$/u.test(term))
  return index.query((builder) => {
    for (const term of terms) {
      const required = numericTerms.includes(term)
      const options = {
        boost: 10,
        usePipeline: false,
        ...(required ? { presence: lunr.Query.presence.REQUIRED } : {}),
      }
      builder.term(term, options)
      if (!required && /^[a-z]{3,}$/u.test(term)) {
        builder.term(term, {
          boost: 3,
          usePipeline: false,
          wildcard: lunr.Query.wildcard.TRAILING,
        })
      }
      if (!required && /^[a-z]{5,}$/u.test(term)) {
        builder.term(term, { boost: 1, usePipeline: false, editDistance: 1 })
      }
    }
  })
}

function exactMatches(documents, query) {
  const foldedQuery = String(query).normalize('NFKC').toLowerCase()
  if (!foldedQuery) return []
  const numericOnly = /^\d+$/u.test(foldedQuery)
  const matches = []
  for (const document of documents) {
    const text = String(document.text || '')
    const foldedText = text.normalize('NFKC').toLowerCase()
    let offset = foldedText.indexOf(foldedQuery)
    let count = 0
    while (offset >= 0 && count < MAX_EXACT_MATCHES_PER_DOCUMENT) {
      if (
        numericOnly
        && ((offset > 0 && /\d/u.test(foldedText[offset - 1]))
          || (offset + foldedQuery.length < foldedText.length && /\d/u.test(foldedText[offset + foldedQuery.length])))
      ) {
        offset = foldedText.indexOf(foldedQuery, offset + Math.max(1, foldedQuery.length))
        continue
      }
      const contextStart = Math.max(0, offset - 500)
      const contextEnd = Math.min(text.length, offset + String(query).length + 1_500)
      matches.push({
        source_id: String(document.id),
        source: String(document.source || 'page'),
        field: String(document.field || 'text'),
        filename: String(document.filename || ''),
        url: String(document.url || ''),
        text: text.slice(contextStart, contextEnd),
        start: offset,
        end: offset + String(query).length,
        exact: true,
        // JSON has no Infinity literal; keep exact hits deterministically ahead
        // of Lunr-ranked fuzzy/term matches without emitting a null score.
        score: 1e9,
        matched_fields: [String(document.field || 'text')],
      })
      count += 1
      offset = foldedText.indexOf(foldedQuery, offset + Math.max(1, foldedQuery.length))
    }
  }
  return matches
}

function lunrMatches(chunks, index, terms) {
  const byRef = new Map(chunks.map((chunk) => [chunk.ref, chunk]))
  return searchIndex(index, terms).map((match) => {
    const chunk = byRef.get(match.ref)
    if (!chunk) return null
    const fields = new Set()
    for (const fieldMatches of Object.values(match.matchData && match.matchData.metadata || {})) {
      for (const field of Object.keys(fieldMatches)) fields.add(field)
    }
    return {
      source_id: chunk.source_id,
      source: chunk.source,
      field: chunk.field,
      filename: chunk.filename,
      url: chunk.url,
      text: chunk.text,
      start: chunk.start,
      end: chunk.end,
      exact: false,
      score: match.score,
      matched_fields: Array.from(fields).sort(),
    }
  }).filter(Boolean)
}

function deduplicate(matches) {
  const selected = []
  const seen = new Set()
  for (const match of matches) {
    const key = `${match.source_id}:${match.start}:${match.end}:${match.exact}`
    if (seen.has(key)) continue
    seen.add(key)
    if (!match.exact && selected.some((item) => item.source_id === match.source_id && item.start <= match.end && match.start <= item.end)) {
      continue
    }
    selected.push(match)
    if (selected.length >= RESULT_LIMIT) break
  }
  return selected
}

function search(payload) {
  const rawDocuments = Array.isArray(payload.documents) ? payload.documents : []
  const documents = rawDocuments.filter((document) => document && typeof document === 'object')
  const normalizedQuery = queryTerms(payload.query || '')
  const chunks = documents.flatMap(splitChunks)
  const index = buildIndex(chunks)
  const exact = exactMatches(documents, normalizedQuery.bounded)
  const ranked = lunrMatches(chunks, index, normalizedQuery.terms)
  const results = deduplicate([...exact, ...ranked])
  return {
    search_mode: 'lunr',
    query: normalizedQuery.bounded,
    query_truncated: normalizedQuery.truncated,
    indexed_documents: documents.length,
    indexed_chunks: chunks.length,
    exact_match_count: exact.length,
    results,
  }
}

function main() {
  const source = require('fs').readFileSync(0, 'utf8')
  const payload = JSON.parse(source)
  process.stdout.write(JSON.stringify(search(payload)))
}

if (require.main === module) main()

module.exports = { search }
