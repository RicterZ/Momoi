import readline from 'node:readline'
import { WechatyBuilder, ScanStatus } from 'wechaty'

const bot = WechatyBuilder.build({
  name: 'momoi-wechaty',
  puppet: 'wechaty-puppet-wechat4u',
  puppetOptions: { uos: true },
})
let nextId = 1
const pending = new Map()

function emit(value) {
  process.stdout.write(JSON.stringify(value) + '\n')
}

bot.on('scan', (qrcode, status) => emit({ type: 'scan', status: ScanStatus[status], qrcode }))
bot.on('login', user => emit({ type: 'login', user: String(user) }))
bot.on('logout', user => emit({ type: 'logout', user: String(user) }))
bot.on('error', error => emit({ type: 'error', message: error?.message || String(error) }))
bot.on('message', async message => {
  try {
    if (message.self() || message.room()) return
    const sender = message.from()
    if (!sender) return
    emit({
      type: 'message', id: String(message.id), senderId: String(sender.id),
      senderName: sender.name(), text: message.text(),
    })
  } catch (error) {
    emit({ type: 'error', message: error?.message || String(error) })
  }
})

const rl = readline.createInterface({ input: process.stdin })
rl.on('line', async line => {
  let command
  try { command = JSON.parse(line) } catch { return }
  if (command.command === 'start') {
    try { await bot.start(); emit({ type: 'started' }) }
    catch (error) { emit({ type: 'error', message: error?.message || String(error) }) }
  } else if (command.command === 'send') {
    try {
      const contact = await bot.Contact.find({ id: String(command.recipient) })
      if (!contact) throw new Error('contact_not_found')
      await contact.say(String(command.text))
      emit({ type: 'response', id: command.id, ok: true })
    } catch (error) {
      emit({ type: 'response', id: command.id, ok: false, error: error?.message || String(error) })
    }
  } else if (command.command === 'cancel') {
    try { await bot.stop() } finally { emit({ type: 'login_cancelled' }) }
  } else if (command.command === 'stop') {
    try { await bot.stop() } finally { process.exit(0) }
  }
})

process.on('SIGTERM', async () => { try { await bot.stop() } finally { process.exit(0) } })
emit({ type: 'ready' })
