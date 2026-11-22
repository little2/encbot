幫我驗証代碼是否符合以下的業務流程

媒體儲存

User A 傳送媒體給 Aiogram Bot B
Bot B 再傳給 Telethon Userbot C

媒體索取

User E 向 Aiogram Bot B 索取媒體, Bot B會讓 BOT D 傳送給 USER E
若Bot D 不存在此媒體，則Bot D 索取 Telethon Userbot C 的媒體
若UserBot C 不存在此媒體， Telethon Userbot C 向 Bot B 索取媒體
若 Bot B 不存在此媒體，Bot B 向 X-MAN 索取媒體

前提假設: 只要 Bot 取得 Media, 就可以用相同的 File ID 來取 Media

列出有做什麼修改，並列出修改的優先順序