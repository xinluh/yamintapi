import requests
import time
import getpass
import json
import re
import os
import random
from itertools import islice
from functools import lru_cache
from datetime import datetime, date
from typing import Sequence as Seq, Mapping
import logging

logger  = logging.getLogger(__name__)

_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9) AppleWebKit/537.71 (KHTML, like Gecko) Version/7.0 Safari/537.71'
_MINT_ROOT_URL = 'https://mint.intuit.com'


class Mint():
    def __init__(self):
        self._js_token = None
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': _USER_AGENT})

    def initiate_account_refresh(self):
        self.session.post(os.path.join(_MINT_ROOT_URL, 'refreshFILogins.xevent'), data={'token': self._js_token})

    def refresh_accounts(self, max_wait_time=60, refresh_every=10) -> dict:
        """Initiate an account refresh and wait for the refresh to finish.
        Returns None if timed out.
        """
        self.initiate_account_refresh()
        for _ in range(max_wait_time//refresh_every):
            data = self._get_json_response('userStatus.xevent', params={'rnd': random.randint(0, 10**14)}, method='get')
            if data['isRefreshing'] is False:
                return data
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

    def get_transactions(self, include_investment=True, limit=None, do_basic_cleaning=True) -> Seq[dict]:
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
        transactions = (islice(transactions, limit) if limit else transactions)
        if not do_basic_cleaning:
            return list(transactions)

        def fix_date(date_str):
            # Mint returns dates like 'Feb 23' for transactions in the current year; reformat to standard date instead
            return (date_str if '/' in date_str
                    else datetime.strptime(date_str + str(date.today().year), '%b %d%Y').strftime('%m/%d/%y'))

        def clean_up(trans):
            for date_key in ('date', 'odate'):
                trans[date_key] = fix_date(trans[date_key])
            trans['amount'] = float(trans['amount'].strip('$').replace(',', '')) * (-1 if trans['isDebit'] else 1)
            return trans

        return list(map(clean_up, transactions))

    def get_transactions_csv(self, include_investment=True) -> str:
        '''
        Return csv result from "Export all transaction" link in the transaction page. Result can be read with the `csv`
        module or `pandas`.

        This contains less detail than get_transactions() but is significantly faster.
        '''
        return self.session.get(os.path.join(_MINT_ROOT_URL, 'transactionDownload.event') +
                                ('?accountId=0' if include_investment else '')).content

    def update_transaction(self,
                           transaction_id: int,
                           description: str = None,
                           category_name: str = None, category_id: int = None,
                           note: str = None,
                           transaction_date: date = None,
                           tags: Mapping[str, bool] = {}) -> dict:
        '''
        transaction_id can be obtained from get_transactions()

        To add/remove tag, pass `tags={'tag_name': True/False}`. Tags not present in `tags` will remain unchanged.

        Only one of category_name and category_id is needed (category_id takes priority). Usually category_name
        suffices, unless there are multiple categories with the same name (but under different parent categories).
        '''
        if not category_id and category_name:
            category_id = self.category_name_to_id(category_name)

        data = {
            'task': 'txnedit', 'token': self._js_token,
            'txnId': '{}:0'.format(transaction_id),
            'note': note,
            'merchant': description,
            'catId': category_id,
            'date': transaction_date.strftime('%m/%d/%Y') if transaction_date else None,
        }

        for tag, checked in tags.items():
            data['tag{}'.format(self.tag_name_to_id(tag))] = 2 if checked else 0

        return self._get_json_response('updateTransaction.xevent', data={k: v for k, v in data.items() if v is not None})

    def add_cash_transaction(self,
                             description: str,
                             amount: float,
                             category_name: str = None, category_id: int = None,
                             note: str = None,
                             transaction_date: date = None,
                             tags: Seq[str] = []) -> dict:
        '''
        If amount if positive, transaction will be created as an income. Else, it is created as an expense.

        Only one of category_name and category_id is needed (category_id takes priority). Usually category_name
        suffices, unless there are multiple categories with the same name (but under different parent categories).
        '''
        if not category_id and category_name:
            category_id = self.category_name_to_id(category_name)

        data = {'txnId': ':0', 'task': 'txnadd', 'token': self._js_token, 'mtType': 'cash',
                'mtCashSplitPref': 2,  # unclear what this is
                'note': note,
                'catId': category_id,
                'amount': abs(amount),
                'mtIsExpense': True if amount < 0 else False,
                'merchant': description,
                'date': (transaction_date or date.today()).strftime('%m/%d/%Y')}

        for tag in tags:
            data['tag{}'.format(self.tag_name_to_id(tag))] = 2

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

    def category_name_to_id(self, category_name, parent_category_name=None) -> int:
        categories = [c for c in self.get_categories() if c['name'] == category_name]
        if not parent_category_name and len(categories) > 1:
            raise RuntimeError('Multiple categories with the same name {} is found. '.format(category_name) +
                               'Need to supply parent category name: {}'.format({c['parent']['name'] for c in categories}))

        res = next((c['id'] for c in categories
                    if not parent_category_name or c['parent']['name'] == parent_category_name), None)

        if not res:
            raise RuntimeError('category {} does not exist'.format(category_name))
        return res

    @lru_cache()
    def get_tags(self) -> dict:
        ''' Return dict keyed by tag name, values are more information about the tag (including id) '''

        # alternative api with less detail: list(self._get_jsondata_response_generator({'task': 'tags'}))
        data = {"args": {},
                "service": "MintTransactionService",
                "task": "getTagsByFrequency"}
        return {t['name']: t for t in self._get_service_response(data)}

    def tag_name_to_id(self, name) -> int:
        tag_id = self.get_tags().get(name, {}).get('id', None)
        if not tag_id:
            raise RuntimeError('Tag {} does not exist. Create it first with create_tag()'.format(name))
        return tag_id

    def create_tag(self, name) -> int:
        ''' Return the id of newly created tag'''
        if name in self.get_tags():
            raise Exception('{} is already a tag'.format(name))
        data = {'nameOfTag': name, 'task': 'C', 'token': self._js_token}
        result = self.session.post(os.path.join(_MINT_ROOT_URL, '/updateTag.xevent'), data=data).text
        try:
            return int(re.match(r'<tagId>([0-9]+)</tagId>', result)[1])
        except TypeError:
            raise RuntimeError('Received unexpected response ' + result)

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
        from selenium.common.exceptions import ElementNotVisibleException, NoSuchElementException
        webdriver.DesiredCapabilities.PHANTOMJS['phantomjs.page.customHeaders.User-Agent'] = _USER_AGENT
        webdriver.DesiredCapabilities.PHANTOMJS['phantomjs.page.settings.userAgent'] = _USER_AGENT

        # PhantomJS sometimes is wonky with SSL and returns an empty page:
        # https://stackoverflow.com/questions/29463603/phantomjs-returning-empty-web-page-python-selenium
        driver = webdriver.PhantomJS(service_args=['--ssl-protocol=TLSv1', '--ignore-ssl-errors=true'])
        if debug:
            self._driver = driver
        driver.set_window_size(1280, 768)
        driver.implicitly_wait(30)

        overview_url = os.path.join(_MINT_ROOT_URL, 'overview.event')
        driver.get(overview_url)

        def wait_and_click_by_id(elem_id, timeout=10, check_freq=1):
            ''' more debug message and finer control over selenium's wait functionality '''
            for _ in range(timeout // check_freq):
                try:
                    element = driver.find_element_by_id(elem_id)
                    if element.is_displayed and element.is_enabled:
                        element.click()
                        return element
                except NoSuchElementException:
                    pass
                time.sleep(check_freq)
                logger.debug('Waiting for id={} to be clickable'.format(elem_id))
            return element

        logger.info('Waiting for login page to load...')

        wait_and_click_by_id('ius-userid').send_keys(email)
        wait_and_click_by_id('ius-password').send_keys(password)
        wait_and_click_by_id('ius-sign-in-submit-btn')

        logger.info('Logging in...')
        while not driver.current_url.startswith(overview_url):
            if 'a code to verify your info' in driver.page_source:
                self._two_factor_login(driver)

            try:
                element = driver.find_element_by_id('ius-verified-user-update-btn-skip')
                if element.is_displayed and element.is_enabled:
                    element.click()
                    logger.info('Skipping phone verification step')
            except (NoSuchElementException, ElementNotVisibleException):
                pass

            time.sleep(2)
            logger.debug('Current page title: ' + driver.title)

        self._js_token = json.loads(driver.find_element_by_id('javascript-user').get_attribute('value'))['token']

        for cookie_json in driver.get_cookies():
            self.session.cookies.set(**{k: v for k, v in cookie_json.items() if k not in ['httponly', 'expiry', 'expires', 'domain']})

        if not debug:
            driver.close()

        self.get_accounts.cache_clear()
        self.get_categories.cache_clear()
        self.get_tags.cache_clear()
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
            results = self._get_json_response('app/getJsonData.xevent', params=params, method='get')['set'][0].get('data', [])
            offset += len(results)
            for result in results:
                yield result
            if not results:
                break

    def _two_factor_login(sel, driver: 'selenium.webdriver'):
        driver.implicitly_wait(3)
        driver.find_element_by_id('ius-mfa-option-email').click()
        driver.find_element_by_id('ius-mfa-options-submit-btn').click()
        driver.find_element_by_id('ius-mfa-confirm-code').send_keys(getpass.getpass('Enter 2 factor code sent to your email: '))
        driver.find_element_by_id('ius-mfa-otp-submit-btn').click()
        driver.implicitly_wait(0)


class MintSessionExpiredException(Exception):
    pass
