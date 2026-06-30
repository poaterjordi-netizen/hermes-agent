'use strict'

const { clipboard, dialog } = require('electron')
const fs = require('fs')
const path = require('path')

const { DATA_URL_READ_MAX_BYTES, resolveReadableFileForIpc, TEXT_PREVIEW_SOURCE_MAX_BYTES } = require('./hardening.cjs')
const { readWslWindowsClipboardImage } = require('./wsl-clipboard-image.cjs')

// File-preview + clipboard + image-save IPC: read a file as a data URL / text
// preview, native file picker, write clipboard text, and persist composer images
// (from URL, raw buffer, or the system clipboard, with a WSL host-clipboard
// fallback). The preview helpers, image writers, and the live main window are
// injected; selectPaths parents its dialog on getMainWindow().
function registerMediaIpc({
  getMainWindow,
  ipcMain,
  IS_WSL,
  looksBinary,
  mimeTypeForPath,
  PREVIEW_LANGUAGE_BY_EXT,
  saveImageFromUrl,
  TEXT_PREVIEW_MAX_BYTES,
  writeComposerImage
}) {
  ipcMain.handle('hermes:readFileDataUrl', async (_event, filePath) => {
    const { resolvedPath } = await resolveReadableFileForIpc(filePath, {
      maxBytes: DATA_URL_READ_MAX_BYTES,
      purpose: 'File preview'
    })
    const data = await fs.promises.readFile(resolvedPath)
    return `data:${mimeTypeForPath(resolvedPath)};base64,${data.toString('base64')}`
  })

  ipcMain.handle('hermes:readFileText', async (_event, filePath) => {
    const { resolvedPath, stat } = await resolveReadableFileForIpc(filePath, {
      maxBytes: TEXT_PREVIEW_SOURCE_MAX_BYTES,
      purpose: 'Text preview'
    })
    const ext = path.extname(resolvedPath).toLowerCase()
    const handle = await fs.promises.open(resolvedPath, 'r')
    const bytesToRead = Math.min(stat.size, TEXT_PREVIEW_MAX_BYTES)

    try {
      const buffer = Buffer.alloc(bytesToRead)
      const { bytesRead } = await handle.read(buffer, 0, bytesToRead, 0)

      return {
        binary: looksBinary(buffer.subarray(0, Math.min(bytesRead, 4096))),
        byteSize: stat.size,
        language: PREVIEW_LANGUAGE_BY_EXT[ext] || 'text',
        mimeType: mimeTypeForPath(resolvedPath),
        path: resolvedPath,
        text: buffer.subarray(0, bytesRead).toString('utf8'),
        truncated: stat.size > TEXT_PREVIEW_MAX_BYTES
      }
    } finally {
      await handle.close()
    }
  })

  ipcMain.handle('hermes:selectPaths', async (_event, options = {}) => {
    const properties = options?.directories ? ['openDirectory'] : ['openFile']
    if (options?.multiple !== false) properties.push('multiSelections')

    let resolvedDefaultPath
    if (options?.defaultPath) {
      try {
        resolvedDefaultPath = path.resolve(String(options.defaultPath))
      } catch {
        resolvedDefaultPath = undefined
      }
    }

    const result = await dialog.showOpenDialog(getMainWindow(), {
      title: options?.title || 'Add context',
      defaultPath: resolvedDefaultPath,
      properties,
      filters: Array.isArray(options?.filters) ? options.filters : undefined
    })

    if (result.canceled) return []
    return result.filePaths
  })

  ipcMain.handle('hermes:writeClipboard', (_event, text) => {
    clipboard.writeText(String(text || ''))
    return true
  })

  ipcMain.handle('hermes:saveImageFromUrl', (_event, url) => saveImageFromUrl(String(url || '')))

  ipcMain.handle('hermes:saveImageBuffer', async (_event, payload) => {
    const data = payload?.data
    if (!data) throw new Error('saveImageBuffer: missing data')

    const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data)
    return writeComposerImage(buffer, payload?.ext || '.png')
  })

  ipcMain.handle('hermes:saveClipboardImage', async () => {
    const image = clipboard.readImage()
    if (image && !image.isEmpty()) {
      return writeComposerImage(image.toPNG(), '.png')
    }

    // WSL2/WSLg doesn't bridge clipboard *images* from the Windows host to the
    // Linux clipboard Electron reads, so a host screenshot looks empty above.
    // Pull it straight off the Windows clipboard via PowerShell as a fallback.
    if (IS_WSL) {
      const png = readWslWindowsClipboardImage()
      if (png) {
        return writeComposerImage(png, '.png')
      }
    }

    return ''
  })
}

module.exports = { registerMediaIpc }
