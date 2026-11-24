幫我驗証代碼是否符合以下的業務流程

媒體儲存

User A 傳送媒體給塔台機器人（即bot，Aiogram Bot B）
此時塔台機器人（Bot B）在用戶（User A）確認後，會傳給停機坪人類機器人　（Telethon Userbot C）


媒體索取

User E 向 塔台機器人（Aiogram Bot B） 索取媒體, 塔台機器人（Bot B）會讓 跑道機器人 ( aiogram bot D ) 傳送給 USER E

C → D
若跑道機器（Bot D） 不存在此媒體，則Bot D 會向　停機坪人類機器人 （Telethon Userbot C）索取媒體


B → C
若　UserBot C 不存在此媒體或是發送給 bot D 發生錯誤或是C 找不到與 B 的私聊時， 則　Telethon Userbot C 向 塔台機器人（Bot B） 索取媒體
C 缺檔、轉發例外、D 回執逾時或回傳空值，各自都能觸發一次補傳，成功後由 D 交付 E。
既有備份都嘗試完，D 仍未取得可用媒體，就進入 B → C 補傳。
舊的 B 私聊無法解析時，B 仍實際嘗試發送；C 收到後，以新訊息位置完成 C → D → E。

X-MAN → B
若 塔台機器人（Bot B） 不存在此媒體或是發送給 UserBot C  發生錯誤，Bot B 向 X-MAN 索取媒體
B 缺檔或明確發送失敗時，由 B 請求 X-MAN 一次，取回後續走 B → C → D → E。回執逾時先核對 C 是否已收到，避免重複補傳。

前提假設: 
（１）只要 Bot 取得 Media, 就可以用相同的 File ID 來取 Media
（２）E 沒有設定跑道 token 時，程式會由 B 直接發給 E；`USERBOT_ENABLED=false` 時，D 缺檔會由 D 直接向 X-MAN 索取。
（２）「A 確認後，若啟用ｃ（`USERBOT_ENABLED=true` ）需確保備份給 C」

列出有做什麼修改，並列出修改的優先順序



