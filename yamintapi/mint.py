import requests
import time
import getpass
import json
import re
import os
import random
from itertools import islice
from functools import lru_cache
from datetime import date
from typing import Sequence as Seq

_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9) AppleWebKit/537.71 (KHTML, like Gecko) Version/7.0 Safari/537.71'
_MINT_ROOT_URL = 'https://mint.intuit.com'


class Mint():
    def __init__(self):
        self.session = requests.Session()
        self._js_token = None
        self.session.headers.update({'User-Agent': _USER_AGENT})

    def initiate_account_refresh(self):
        self.session.post(os.path.join(_MINT_ROOT_URL, 'refreshFILogins.xevent'), data={'token': self._js_token})

    def refresh_accounts(self, max_wait_time=60, refresh_every=10) -> dict:
        """Initiate an account refresh and wait for the refresh to finish.
        Returns None if timed out.
        """
        self.initiate_account_refresh()
        waited = 0
        while True:
            data = self._get_json_response('userStatus.xevent', params={'rnd': random.randint(0, 10**14)}, method='get')
            if data['isRefreshing'] is False:
                return data
            elif waited > max_wait_time/refresh_every:
                return None
            else:
                waited += 1
                time.sleep(refresh_every)

    @lru_cache()
    def get_accounts(self) -> Seq[dict]:
        params = {
            'args': {
                'types': ['BANK', 'CREDIT', 'INVESTMENT', 'LOAN', 'MORTGAGE', 'OTHER_PROPERTY', 'REAL_ESTATE', 'VEHICLE', 'UNCLASSIFIED']
            },
            'service': 'MintAccountService',
            'task': 'getAccountsSorted'
        }

        return self._get_service_response(params)

    def get_transactions(self, include_investment=True, limit=None) -> Seq[dict]:
        '''
        Return detailed transactions. Suggest running with e.g. get_transactions(limit=100) since getting all transactions is
        a slow operation.
        '''
        params = {'queryNew': None,
                  'comparableType': 8,
                  'task': 'transactions'}
        if include_investment:
            params['accountId'] = 0
        else:
            params['task'] = 'transactions,txnfilter'
            params['filterType'] = 'cash'

        transactions = self._get_jsondata_response_generator(params)
        return list(islice(transactions, limit) if limit else transactions)

    def get_transactions_csv(self, include_investment=True) -> str:
        '''
        Return csv result from "Export all transaction" link in the transaction page. Result can be read with the `csv`
        module or `pandas`.

        This contains less detail than get_transactions() but is significantly faster.
        '''
        return self.session.get(os.path.join(_MINT_ROOT_URL, 'transactionDownload.event') +
                                ('?accountId=0' if include_investment else '')).content

    def update_transaction(self, transaction_id, description=None, category_id=None, note=None, transaction_date: date = None) -> dict:
        '''
        transaction_id can be obtained from get_transactions() and category_id can be obtained from category_name_to_id(...)
        '''
        data = {
            'task': 'txnedit', 'token': self._js_token,
            'txnId': '{}:0'.format(transaction_id),
            'note': note,
            'merchant': description,
            'catId': category_id,
            'date': transaction_date.strftime('%m/%d/%Y') if transaction_date else None,
        }
        return self._get_json_response('updateTransaction.xevent', data={k: v for k, v in data.items() if v})

    def add_cash_transaction(self, description, amount, category_id=None, note=None, transaction_date=None, is_expense=True) -> dict:
        data = {'txnId': ':0', 'task': 'txnadd', 'token': self._js_token, 'mtType': 'cash',
                'mtCashSplitPref': 2,  # unclear what this is
                'note': note,
                'catId': category_id,
                'amount': amount,
                'mtIsExpense': is_expense,
                'merchant': description,
                'date': (transaction_date or date.today()).strftime('%m/%d/%Y')}
        return self._get_json_response('updateTransaction.xevent', data={k: v for k, v in data.items() if v})

    @lru_cache()
    def get_categories(self) -> Seq[dict]:
        data = {
            'args': {
                'excludedCategories': [],
                'sortByPrecedence': False,
                'categoryTypeFilter': 'FREE'
            },
            'service': 'MintCategoryService',
            'task': 'getCategoryTreeDto2'
        }
        return self._get_service_response(data)['allCategories']

    def category_id_to_name(self, category_id) -> str:
        categories = self.get_categories()
        return next((c['name'] for c in categories if c['id'] == category_id), None)

    def category_name_to_id(self, category_name, parent_category_name=None) -> int:
        categories = self.get_categories()
        return next((c['id'] for c in categories if c['name'] == category_name and
                     (not parent_category_name or c['parent']['name'] == parent_category_name)), None)

    def get_tags(self) -> Seq[dict]:
        data = {"args": {},
                "service": "MintTransactionService",
                "task": "getTagsByFrequency"}
        return self._get_service_response(data)

    def set_user_property(self, name, value) -> bool:
        params = {'args': {'propertyName': name,
                           'propertyValue': value},
                  'service': 'MintUserService',
                  'task': 'setUserProperty'}
        return self._get_service_response(params)

    def login(self, email, password, debug=False) -> 'Mint':
        '''Use selenium + phantomjs to get login cookies and token.

        You should run this function interactively at least once so you can supply the 2 factor authentication
        code interactively.

        If debug=True, you can access the webdriver used at `Mint._driver` for debugging to see the current page.
        A few useful functions: `Mint._driver.page_source`, `Mint._driver.get_screenshot_as_file('/tmp/test.png')`

        '''
        from selenium import webdriver
        webdriver.DesiredCapabilities.PHANTOMJS['phantomjs.page.customHeaders.User-Agent'] = _USER_AGENT
        webdriver.DesiredCapabilities.PHANTOMJS['phantomjs.page.settings.userAgent'] = _USER_AGENT

        driver = webdriver.PhantomJS()
        if debug:
            self._driver = driver
        driver.set_window_size(1120, 550)
        driver.implicitly_wait(30)

        overview_url = os.path.join(_MINT_ROOT_URL, 'overview.event')
        driver.get(overview_url)

        if debug:
            print('Waiting for login page to load...')

        driver.find_element_by_id("ius-userid").click()
        driver.find_element_by_id("ius-userid").send_keys(email)
        driver.find_element_by_id("ius-password").send_keys(password)
        driver.find_element_by_id("ius-sign-in-submit-btn").submit()
        if debug:
            print('Logging in...')

        while not driver.current_url.startswith(overview_url):
            if 'a code to verify your info' in driver.page_source:
                self._two_factor_login(driver)
            time.sleep(1)
            if debug:
                print(driver.current_url)

        self._js_token = json.loads(driver.find_element_by_id('javascript-user').get_attribute('value'))['token']

        new_cookies = requests.cookies.RequestsCookieJar()
        for cookie_json in driver.get_cookies():
            new_cookies.set(**{k: v for k, v in cookie_json.items() if k not in ['httponly', 'expiry', 'expires', 'domain']})
        self.session.cookies = new_cookies

        if not debug:
            driver.close()

        self.get_accounts.cache_clear()
        self.get_categories.cache_clear()
        return self

    @property
    def is_logged_in(self) -> bool:
        return self._js_token is not None

    def _get_json_response(self, url, params: dict = None, data: dict = None, method='post') -> dict:
        response = self.session.request(method=method,
                                        url=os.path.join(_MINT_ROOT_URL, url),
                                        params=params,
                                        data=data,
                                        headers={'accept': 'application/json'})

        self._last_request_result = response.text

        if (response.status_code != requests.codes.ok or
           not re.match('text/json|application/json', response.headers.get('content-type', ''))):
            if 'session has expired' in response.text.lower():
                raise MintSessionExpiredException()
            else:
                raise RuntimeError('Request for {} {} {} failed: {} {}'.format(url, params, data, response.status_code, response.headers))

        return json.loads(response.text)

    def _get_service_response(self, data: dict) -> dict:
        data = data.copy()
        data['id'] = str(random.randint(0, 10**14))

        result = self._get_json_response('bundledServiceController.xevent',
                                         params={'legacy': False, 'token': self._js_token},
                                         data={'input': json.dumps([data])})

        if data['id'] not in result.get('response', []):
            raise RuntimeError('bundleServiceController request for {} failed, response: {} {}'.format(data, result, result.text))

        return result['response'][data['id']]['response']

    def _get_jsondata_response_generator(self, params, initial_offset=0):
        params = params.copy()
        offset = initial_offset
        while True:
            params['offset'] = offset
            params['rnd'] = random.randint(0, 10**14)
            results = self._get_json_response('getJsonData.xevent', params=params, method='get')['set'][0].get('data', [])
            offset += len(results)
            for result in results:
                yield result

    def _two_factor_login(sel, driver: 'selenium.webdriver'):
        driver.find_element_by_id('ius-mfa-option-email').click()
        driver.find_element_by_id('ius-mfa-options-submit-btn').click()
        driver.find_element_by_id('ius-mfa-confirm-code').send_keys(getpass.getpass('Enter 2 factor code sent to your email: '))
        driver.find_element_by_id('ius-mfa-otp-submit-btn').click()


class MintSessionExpiredException(Exception):
    pass
