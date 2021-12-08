# yamintapi: Yet Another Mint.com API

A minimalistic wrapper to Mint.com API, designed to be a lean but functional module that other Python applications can use. It has plenty of editing functionality such as add / change transactions, add tag, add/change custom category, etc.

## Requirements

- Python 3+
- Only real dependency is `requests`
- For automated login, `selenium` and Chrome / [ChromeDriver](https://chromedriver.chromium.org/) are required. Alternatively, login information can be updated using information from a browser session.

## Installation
```
python -m pip install git+https://github.com/xinlu/yamintapi.git@master
```
Or add to `requirements.txt` or `pyproject.toml` etc. depending on your package manager.

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

### Add / change custom category
```python
new_category_id = mint.create_category('boba tea shop', 2)  # 2 is the category id for the "Food & Dining" category
mint.rename_category(new_category_id, 'specialized boba shop')
mint.rename_category(new_category_id)
```

## 2-factor setup
If the script using Mint is running headlessly, then it is essential to set up 2 factor. One way (but not the only way) to set this one:
1. Turn on "Authenticator App" 2 factor authentication in Mint (https://accounts-help.intuit.com/app/intuit/1995123)
2. At the step where you are asked to scan the QR code, **make sure that you copy the manual setup code** and safeguard it just like you safeguard your password.
3. You can use the commandline tool `oathtool` to get 2 factor code, e.g.
```python
import subprocess
def get_2fa():
  subprocess.check_output(['oathtool', '--totp', '--base32', <YOUR_2FA_SETUP_CODE>])

mint = Mint().login(email, password, get_two_factor_code_func=get_2fa)
```

## See also

[mintapi](https://github.com/mrooney/mintapi)
