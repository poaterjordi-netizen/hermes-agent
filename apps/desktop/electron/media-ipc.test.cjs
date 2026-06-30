'use strict'

const assert = require('node:assert/strict')
const test = require('node:test')

const { registerMediaIpc } = require('./media-ipc.cjs')

function fakeIpcMain() {
  const handlers = new Map()

  return {
    handlers,
    handle(channel, handler) {
      assert.ok(!handlers.has(channel), `duplicate registration for ${channel}`)
      handlers.set(channel, handler)
    }
  }
}

function deps(overrides = {}) {
  return {
    getMainWindow: () => null,
    IS_WSL: false,
    looksBinary: () => false,
    mimeTypeForPath: () => 'text/plain',
    PREVIEW_LANGUAGE_BY_EXT: {},
    saveImageFromUrl: async () => '/img/from-url.png',
    TEXT_PREVIEW_MAX_BYTES: 1024,
    writeComposerImage: async () => '/img/written.png',
    ...overrides
  }
}

test('registerMediaIpc wires the file-preview / clipboard / image-save channels', () => {
  const ipcMain = fakeIpcMain()

  registerMediaIpc({ ipcMain, ...deps() })

  assert.deepEqual([...ipcMain.handlers.keys()].sort(), [
    'hermes:readFileDataUrl',
    'hermes:readFileText',
    'hermes:saveClipboardImage',
    'hermes:saveImageBuffer',
    'hermes:saveImageFromUrl',
    'hermes:selectPaths',
    'hermes:writeClipboard'
  ])

  for (const handler of ipcMain.handlers.values()) {
    assert.equal(typeof handler, 'function')
  }
})

test('saveImageFromUrl delegates to the injected writer', async () => {
  const ipcMain = fakeIpcMain()
  const seen = []

  registerMediaIpc({
    ipcMain,
    ...deps({
      saveImageFromUrl: async url => {
        seen.push(url)

        return '/p.png'
      }
    })
  })

  assert.equal(await ipcMain.handlers.get('hermes:saveImageFromUrl')({}, 'https://x/y.png'), '/p.png')
  assert.deepEqual(seen, ['https://x/y.png'])
})

test('saveImageBuffer rejects missing data and otherwise writes via the injected writer', async () => {
  const ipcMain = fakeIpcMain()
  const writes = []

  registerMediaIpc({
    ipcMain,
    ...deps({
      writeComposerImage: async (buf, ext) => {
        writes.push([buf.toString('utf8'), ext])

        return '/written.gif'
      }
    })
  })

  await assert.rejects(() => ipcMain.handlers.get('hermes:saveImageBuffer')({}, {}), /missing data/)

  const out = await ipcMain.handlers.get('hermes:saveImageBuffer')({}, { data: Buffer.from('hi'), ext: '.gif' })

  assert.equal(out, '/written.gif')
  assert.deepEqual(writes, [['hi', '.gif']])
})
