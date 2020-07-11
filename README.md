# yamintapi: Yet Another Mint.com API

A minimalistic wrapper to Mint.com API, designed to be a lean but functional module that other Python applications can use.

## Requirements

- Python 3+
- Only real dependency is `requests`
- For automated login, `selenium` and Chrome / [ChromeDriver](https://chromedriver.chromium.org/) are required. Alternatively, login information can be updated using information from a browser session.

## Usage

### Log in
```python
from yamintapi import Mint
import getpass

# will prompt for 2-factor login code that will be sent to email
mint = Mint().login(email, password, get_two_factor_code_func=lambda: getpass.getpass("Enter 2 factor code sent to your email: "))

mint.refresh_accounts()
```
This requires user interaction whenever 2-factor code is required. See "Advanced 2-factor setup" section below for some strategy for a completely non-interactive way to bypass 2-factor interaction.

Alternatively, logging in using an existent browser session with no selenium/phantomjs dependency
```python
mint = Mint()
mint._js_token = ... # find the attribute value for element id=javascript-user from any page in Mint after logging in
# for each cookie in mint session
mint.session.cookies[...] = ... 
```

### Get information
```python
mint.get_accounts()
mint.get_categories()
mint.get_tags()
```
The results of these calls are cached. To clear the cache, just call `mint.login(...)` again.

### Get transactions
```python
# newest 100 transactions
mint.get_transactions(limit=100)

# all transactions (slow!)
mint.get_transactions()
```

Another option to get all transactions, much faster but with less details:
```python
import io
csv_string = mint.get_transaction_csv()

# then parse with 
import csv
transactions = [row for row in csv.DictReader(io.StringIO(csv_string))]

# OR with pandas
import pandas as pd
transactions = pd.read_csv(io.StringIO(csv_string))
```

### Change/add transactions
Change description, add `tag1` and remove `tag2` to the newest transaction
```python
trans_id = mint.get_transactions(limit=1)[0]['id']
mint.update_transaction(trans_id, description='new description', tags={'tag1': True, 'tag2: False'})
```
Tags should already exist. If not, you can create with `mint.add_tag(tag_name)`.

Add a cash transaction (default to today)
```python
mint.add_cash_transaction('dinner', amount=-10.0, category_name='Restaurants', tags=['tag1', 'tag2'])
```


## Advanced 2-factor setup
For using this library in a way that doesn't require any user interaction (for example, a cronjob in a server), 2-factor verification may be a blocker, especially if Mint server determined that a certain IP address is high risk and frequently requires 2-factor code.

General strategy is to forward the 2 factor code from your email to your trusted server, which pauses and waits for code during the Mint login process via the `get_two_factor_code_func` option in `Mint().login(...)` function.

An example function is included: the follow code pauses the login process to start a temporary http server at port 2222 that waits up to 120 seconds for a GET request that looks like `/mintcode?<2-factor-code>`
```python
from yamintapi import Mint, wait_for_code_via_http

mint = Mint().login(
  email, password, 
  get_two_factor_code_func=lambda: wait_for_code_via_http(port=2222, url_keyword='mintcode', timeout=120)
)
```

Next step is to forward the 2 factor code. With Gmail, you can use [App Script](https://script.google.com/) to do that automatically in response to new emails.  You want to schedule this script to be run around the same time that your server script is running. 

This is an example script that looks for an email with subject "Your Mint Account" then sends the http request with the 2-factor code:
```js
function forwardMintMail() { 
  const urlBase = 'http://<your-server-ip>:2222/mintcode?'
  
  const threads = GmailApp.getInboxThreads()
  for (var x in threads) {
    var thread = threads[x];
    if (thread.getFirstMessageSubject() !== "Your Mint Account") {
      continue; 
    }    
    var msgs = thread.getMessages()
    for (var j in msgs) {
      var msg = msgs[j];
      if (!msg.isUnread()) {
        continue;
      }
      
      var match = msg.getPlainBody().match(/[0-9]{6}/);
      if (match) {
        var verificationCode = match[0];
        const url = urlBase + verificationCode;
        try {
          var response = UrlFetchApp.fetch(url, {'muteHttpExceptions': true});
          // optional: mark 2-factor email as read and archive if successfully sent
          msg.markRead();
          thread.moveToArchive();
        } catch(e) {
          Logger.log('Failed to send code')  
        }
      }
    }
  }  
}
```

The process in sequence diagram:
![image](https://user-images.githubusercontent.com/9114601/87220979-985b6c00-c336-11ea-941a-094fc46abb09.png)


## See also

[mintapi](https://github.com/mrooney/mintapi)
