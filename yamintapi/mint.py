import requests
import time
import html
import getpass
import json
import re
import os
from pathlib import Path
import pickle
import random
from itertools import islice
from functools import lru_cache
from datetime import datetime, date
from typing import Sequence as Seq, Mapping, Union, List
import logging


logger  = logging.getLogger(__name__)


_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9) AppleWebKit/537.71 (KHTML, like Gecko) Version/7.0 Safari/537.71'
_MINT_ROOT_URL = 'https://mint.intuit.com'

class Mint():
    def __init__(self):
        self._js_token = None
        self._init_session()

    def _init_session(self, cookies=None):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': _USER_AGENT})

        if cookies:
            self.session.cookies = cookies

    def initiate_account_refresh_all(self):
        providers = self.get_financial_providers()
        links = providers.get('metaData', {}).get('link', {}) or []

        refresh_ops = next((l for l in links if l.get('operation') == 'refreshAllProviders'), None)

        if not refresh_ops:
            raise RuntimeError('initiate_account_refresh failed: {}'.format(providers.get('metaData')))

        res = self._get_financial_provider_response(refresh_ops['href'], method='post')

        return res.json()

    def initiate_account_refresh(self, fi_id):
        """
        fi_id is the `fiLoginId` key in the get_accounts() output
        """
        provider = self._get_provider(fi_id)
        refresh_url = next((l['href'] for l in provider['metaData']['link'] if l['operation'] == 'refreshProvider'), None)
        get_url = next((l['href'] for l in provider['metaData']['link'] if l['operation'] == 'self'), None)

        if not refresh_url:
            raise RuntimeError('Unexpected provider format: {}'.format(provider))

        params = {
            "requestParams":{"selectors":{"accountSelector":{},"billSelector":{},"transactionSelector":{}}},
            "providers":[{
                "providerId": provider['staticProviderRef']['id'],
                "credentialSets":[
                    {"credentialSetId":provider['cpProviderId']
                    }
                ]
            }]
        }

        self._get_financial_provider_response(refresh_url, method='post', data=params)

        self._get_financial_provider_response('https://mint.intuit.com/pfm/v1/fdpa/provision/ticket', method='put')

        return self._get_financial_provider_response(get_url).json()

    def refresh_accounts(self) -> dict:
        """Initiate an account refresh and wait for the refresh to finish.
        Returns None if timed out.
        """
        self.initiate_account_refresh_all()

    def get_accounts(self) -> Seq[dict]:
        params = {
            'args': {
                'types': ['BANK', 'CREDIT', 'INVESTMENT', 'LOAN', 'MORTGAGE', 'OTHER_PROPERTY', 'REAL_ESTATE', 'VEHICLE', 'UNCLASSIFIED']
            },
            'service': 'MintAccountService',
            'task': 'getAccountsSorted'
        }
        return self._get_service_response(params)

    def get_financial_providers(self) -> dict:
        return self._get_financial_provider_response('/v1/providers').json()

    def _get_provider(self, fi_id) -> dict:
        # Provider ids looks like `PFM:{user_id}_{fi_id}`
        providers = self.get_financial_providers().get('providers', [])

        def get_id(provider, default=None):
            return next((d.get('id') for d in provider.get('domainIds', []) if d.get('domain') == 'PFM'), default)

        provider = next((p for p in providers if get_id(p, '').endswith('_{}'.format(fi_id))), None)

        if not provider:
            raise RuntimeError('asset not found out of {} providers'.format(len(providers)))

        return provider

    def _get_financial_provider_account(self, acct_id) -> dict:
        providers = self.get_financial_providers().get('providers', [])

        def get_id(acct, default=None):
            return next((d.get('id') for d in acct.get('domainIds', []) if d.get('domain') == 'PFM'), default)

        acct = next(
            acct
            for provider in providers
            for acct in provider.get('providerAccounts', [])
            if get_id(acct, '').endswith('_' + str(acct_id))
        )

        if not acct:
            raise RuntimeError('account {} not found out of {} providers'.format(acct_id, len(providers)))

        return acct

    def set_account_visibility(self, acct_id, visible: bool) -> bool:
        acct_json = self._get_financial_provider_account(acct_id)
        update_url = next((l['href'] for l in acct_json['metaData']['link'] if l['operation'] == 'updateAccount'), None)

        if not update_url:
            raise RuntimeError('Unexpected acct format: {}'.format(acct_json))

        if acct_json['isVisible'] == visible:
            return True

        params = {
            "type": acct_json["type"],
            "cpId": acct_json["cpId"],
            "planningTrendsVisible": visible,
            "isVisible": visible,
            "isBillVisible": visible,
            "isPaymentMethodVisible": visible
        }

        res = self._get_financial_provider_response(update_url, method='PATCH', data=params)

        logger.info('set_account_visibility response: {}'.format(res.text))
        return res.ok

    def update_asset_value(self, fi_id: int, value: float) -> dict:
        """
        Update value for manually entered assets.

        fi_id is the `fiLoginId` key in the get_accounts() output
        """

        provider = self._get_provider(fi_id)
        acct = provider['providerAccounts'][0]
        url = acct['metaData']['link'][0]['href']
        params = {
            "name": acct['name'],
            "type": "OtherPropertyAccount",
            "associatedLoanAccounts": acct['associatedLoanAccounts'],
            "hasAssociatedLoanAccounts": len(acct['associatedLoanAccounts']) > 0,
            "value": value
        }

        res = self._get_financial_provider_response(url, method='patch', data=params)
        return res.status_code < 400

    def _clean_transaction(self, raw_transaction):
        def fix_date(date_str):
            # Mint returns dates like 'Feb 23' for transactions in the current year; reformat to standard date instead
            return (date_str if '/' in date_str
                    else datetime.strptime(date_str + str(date.today().year), '%b %d%Y').strftime('%m/%d/%y'))

        for date_key in ('date', 'odate'):
            raw_transaction[date_key] = fix_date(raw_transaction[date_key])
        raw_transaction['amount'] = float(raw_transaction['amount'].strip('$').replace(',', '')) * (-1 if raw_transaction['isDebit'] else 1)
        return raw_transaction

    def get_transactions(
            self,
            include_investment=True,
            limit=None,
            offset=0,
            sort_field='date',
            sort_ascending=False,
            query=None,
            start_date=None,
            end_date=None,
            account_id=None,
            do_basic_cleaning=True
    ) -> Seq[dict]:
        '''
        Return detailed transactions. Suggest running with e.g. get_transactions(limit=100) since getting all transactions is
        a slow operation.
        '''
        comparableType = {
            ('date', True): 4,
            ('date', False): 8,
            ('amount', True): 7,
            ('amount', False): 3,
            ('merchant', True): 1,
            ('merchant', False): 5,
            ('category', True): 2,
            ('category', True): 6,
        }.get((sort_field, sort_ascending), None)

        if comparableType is None:
            raise ValueError('Sort field {} and ascending {} is not supported'.format(sort_field, sort_ascending))

        params = {
            'queryNew': '',
            'comparableType': comparableType,
            'task': 'transactions',
            'query': query,
            'startDate': start_date,
            'endDate': end_date,
        }

        if account_id is not None:
            params['accountId'] = account_id
            params['acctChanged'] = 'T'
        elif include_investment:
            params['accountId'] = 0
        else:
            params['filterType'] = 'cash'
            params['task'] = 'transactions,txnfilter',


        params = {k: v for k, v in params.items() if v is not None}

        transactions = self._get_jsondata_response_generator(params, initial_offset=offset)
        transactions = (islice(transactions, limit) if limit else transactions)

        if not do_basic_cleaning:
            return list(transactions)
        else:
            return list(map(self._clean_transaction, transactions))


    def get_transactions_csv(self, include_investment=True) -> str:
        '''
        Return csv result from "Export all transaction" link in the transaction page. Result can be read with the `csv`
        module or `pandas`.

        This contains less detail than get_transactions() but is significantly faster.
        '''
        return self.session.get(os.path.join(_MINT_ROOT_URL, 'transactionDownload.event') +
                                ('?accountId=0' if include_investment else '')).content

    def update_transaction(self,
                           transaction_id: Union[int, List[int]],
                           description: str = None,
                           category_name: str = None, category_id: int = None,
                           is_duplicate: bool = None,
                           note: str = None,
                           transaction_date: date = None,
                           tags: Mapping[str, bool] = {},
                           amount: float = None,
    ) -> bool:
        '''
        transaction_id can be obtained from get_transactions().

        It can be a single id, or a list of ids.  Example "12345:0" or "23456:1", where the :0 or :1 ending is txnType
        (0 is cash/credit transaction, 1 is investment transactions). If only the id with txnType suffix is added, the
        transaction will be presumed to be a credit/cash one.

        To add/remove tag, pass `tags={'tag_name': True/False}`. Tags not present in `tags` will remain unchanged.

        Only one of category_name and category_id is needed (category_id takes priority). Usually category_name
        suffices, unless there are multiple categories with the same name (but under different parent categories).

        The amount arg is only valid for cash transactions. The request will fail if trying to modify amount for
        a non-cash transaction.
        '''

        category_id, category_name = self._validate_category(category_id, category_name)

        trans_ids = transaction_id if isinstance(transaction_id, list) else [transaction_id]

        data = {
            'task': 'txnedit', 'token': self._js_token,
            'txnId': ','.join(['{}:0'.format(i) if ':' not in str(i) else i for i in trans_ids]),
            'note': note,
            'merchant': description,
            'catId': category_id,
            'category': category_name,
            'date': transaction_date.strftime('%m/%d/%Y') if transaction_date else None,
            'duplicate': 'on' if is_duplicate == True else None,
            'amount': amount
        }

        for tag, checked in tags.items():
            data['tag{}'.format(self.tag_name_to_id(tag))] = 2 if checked else 0

        params = {k: v for k, v in data.items() if v is not None}
        logger.info('update_transaction {}'.format(params))

        resp = self._get_json_response('updateTransaction.xevent', data=params)
        success = resp.get('task') == 'txnEdit'

        if not success:
            logger.error('update_transaction failed,resp: {}'.format(resp))

        return success


    def _validate_category(self, category_id, category_name) -> [int, str]:
        if category_id is None and category_name is None:
            return category_id, category_name

        if not category_id and category_name:
            category_id = self.category_name_to_id(category_name)

        if category_id is not None:
            category_name = next((c['name'] for c in self.get_categories() if c['id'] == category_id), None)
            if not category_name:
                raise ValueError('{} is not a valid category id'.format(category_id))

        return category_id, category_name


    def split_transaction(self, transaction_id, split_transactions: List[dict]) -> dict:
        """
        Split transactions. Return a list of transaction ids, where the first one is the origianl transactions,
        and subsequent ones are the child transactions.

        To unsplit the transaction, simply provide an empty list to split_transactions.
        If the sum of split_transactions doesn't match the origianl amount, Mint will automatically create
        one more split transaction with the remainder.

        >>> mint.split_transaction('3985739713:0', [
        >>>      {'amount': 10, 'merchant': 'description 1', 'category_name': 'Transfer'},
        >>>      {'amount': 26.34, 'merchant': 'description 2', 'category_name': 'Transfer'}
        >>> ])

        Return the split transactions
        """
        full_trans_id = transaction_id if ':' in transaction_id else '{}:0'.format(transaction_id)

        params = {
            'task': 'split',
            'data': '',
            'txnId': full_trans_id,
            'token': self._js_token,
        }

        for idx, tr in enumerate(split_transactions):
            category_id, category_name = self._validate_category(tr.get('category_id'), tr.get('category_name'))
            if category_id is None:
                raise ValueError('Category id or name is missng or invalid')

            params['amount{}'.format(idx)] = tr['amount']
            params['percentAmount{}'.format(idx)] = tr['amount']
            params['category{}'.format(idx)] = category_name
            params['categoryId{}'.format(idx)] = category_id
            params['merchant{}'.format(idx)] = tr['merchant']
            params['txnId{}'.format(idx)] = 0

        resp = self._get_json_response('updateTransaction.xevent', data=params)

        if resp.get('task') != 'split':
            raise RuntimeError('Split transaction failed: {}'.format(resp))

        split_resp = self._get_json_response('listSplitTransactions.xevent', {
            'txnId': full_trans_id
        }, method='get', unescape_html=True)

        result_trans = split_resp['children'] if len(split_resp['children']) > 0 else split_resp['parent']

        return [self._clean_transaction(t) for t in result_trans]


    def delete_transaction(self, transaction_id: str) -> bool:
        trans = self.get_transaction_by_id(transaction_id)

        if not trans['isPending'] and not trans['account'] == 'Cash':
            raise RuntimeError('transacation_id {} is not a pending or cash transaction. Probably not a good idea to delete'.format(transaction_id))

        full_id = '{}:0'.format(transaction_id) if ':' not in transaction_id else transaction_id
        resp = self._get_json_response('updateTransaction.xevent', data={
            'task': 'delete',
            'txnId': full_id,
            'token': self._js_token,
        })

        return resp.get('task') == 'delete'

    def get_transaction_by_id(self, transaction_id, do_basic_cleaning=True):
        """
        transaction_id can either be a number, e.g. 103867187, in which case it will be assume to be
        of transaction type 0 (cash / bank); or a fully qualified string, e.g. "103867187:1", which is
        required for brokerage transactions.
        """
        full_trans_id = str(transaction_id)
        if ':' not in full_trans_id:
            full_trans_id = '{}:0'.format(full_trans_id)
        else:
            transaction_id = int(full_trans_id.split(":")[0])

        res = self._get_json_response('listSplitTransactions.xevent', {
            'txnId': full_trans_id
        }, method='get', unescape_html=True)

        if 'parent' not in res or 'children' not in res:
            raise RuntimeError('Unexpected output from listSplitTransactions: {}'.format(res))

        if str(res['parent'][0]['id']) == str(transaction_id):
            trans = res['parent'][0]
        else:
            trans = next((t for t in res['children'] if str(t['id']) == str(transaction_id)), None)

        if trans is not None and do_basic_cleaning:
            trans = self._clean_transaction(trans)

        return trans

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

        return self._get_json_response('updateTransaction.xevent', data={k: v for k, v in data.items() if v is not None})

    def _get_category_response(self, data) -> int:
        """
        If successful, return the cateogry id.
        """
        result = self.session.post(os.path.join(_MINT_ROOT_URL, 'updateCategory.xevent'), data=data).text
        try:
            return int(re.search(r'<catId>([0-9]+)</catId>', result)[1])
        except TypeError as e:
            raise RuntimeError('Received unexpected response ' + result) from e

    def _get_category_by_id(self, category_id) -> dict:
        categories = self.get_categories()
        try:
            return [c for c in categories if c['id'] == category_id][0]
        except:
            raise RuntimeError(f'category_id {category_id} seems to not exist')

    def create_category(self, name: str, parent_category_id: int) -> int:
        """ Returns new cateogry id if successful """
        parent_category = self._get_category_by_id(parent_category_id)

        if parent_category['depth'] != 1:
            raise RuntimeError(f'Cannot only create cateogry under sub category: {parent_category}')

        data = {
            'pcatId': parent_category_id,
            'catId': 0,
            'category': name,
            'task': 'C',
            'token': self._js_token,
        }

        return self._get_category_response(data)

    def rename_category(self, category_id: int, name: str) -> bool:
        category = self._get_category_by_id(category_id)

        if category_id < 10000:
            raise RuntimeError('Cannot only change user category')

        data = {
            'pcatId': category['parentId'],
            'catId': category_id,
            'category': name,
            'task': 'U',
            'token': self._js_token,
        }

        return self._get_category_response(data) > 0

    def delete_category(self, category_id: int) -> bool:
        category = self._get_category_by_id(category_id)

        if category_id < 10000:
            raise RuntimeError('Cannot only change user category')

        data = {
            'catId': category_id,
            'task': 'D',
            'token': self._js_token,
        }

        return self._get_category_response(data) > 0

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

    def login(self, email, password, get_two_factor_code_func=None, debug=False) -> 'Mint':
        '''Use selenium + phantomjs to get login cookies and token.

        You should run this function interactively at least once so you can supply the 2 factor authentication
        code interactively.

        If debug=True, you can access the webdriver used at `Mint._driver` for debugging to see the current page.
        A few useful functions: `Mint._driver.page_source`, `Mint._driver.get_screenshot_as_file('/tmp/test.png')`

        '''
        from selenium import webdriver
        from selenium.common.exceptions import ElementNotVisibleException, NoSuchElementException, ElementNotInteractableException, StaleElementReferenceException

        options = webdriver.ChromeOptions()
        if not debug:
            options.add_argument('headless')

        driver = webdriver.Chrome(chrome_options=options)
        if debug:
            self._driver = driver
        driver.set_window_size(1280, 768)
        driver.implicitly_wait(0)

        overview_url = os.path.join(_MINT_ROOT_URL, 'overview.event')
        driver.get(overview_url)

        def wait_and_click_by_id(elem_id, timeout=10, check_freq=1, by_testid=False):
            ''' more debug message and finer control over selenium's wait functionality '''
            for _ in range(timeout // check_freq):
                try:
                    if not by_testid:
                        element = driver.find_element_by_id(elem_id)
                    else:
                        element = driver.find_element_by_xpath(f'//*[@data-testid = "{elem_id}"]')

                    if element.is_displayed and element.is_enabled:
                        element.click()
                        return element
                except NoSuchElementException:
                    pass
                time.sleep(check_freq)
                logger.debug('Waiting for id={} to be clickable'.format(elem_id))

            driver.get_screenshot_as_file('/tmp/mint/login_click_failed.png')
            raise Exception('Fail to find id={} to click on'.format(elem_id))

        logger.info('Waiting for login page to load...')

        try:
            # try old login page first (user name and password on same page)
            wait_and_click_by_id('ius-userid').send_keys(email)
            wait_and_click_by_id('ius-password').send_keys(password)
            wait_and_click_by_id('ius-sign-in-submit-btn')
        except (NoSuchElementException, ElementNotVisibleException, ElementNotInteractableException):
            # new login page
            try:
                wait_and_click_by_id('ius-identifier').send_keys(email)
                wait_and_click_by_id('ius-sign-in-submit-btn')
                wait_and_click_by_id('ius-sign-in-mfa-password-collection-current-password').send_keys(password)
                wait_and_click_by_id('ius-sign-in-mfa-password-collection-continue-btn')
            except Exception:
                driver.get_screenshot_as_file('/tmp/mint/login_input_failed.png')
                raise

        def get_js_token(driver):
            if driver.current_url.startswith(overview_url):
                try:
                    user_elem = driver.find_element_by_id('javascript-user')
                except NoSuchElementException:
                    return None
                else:
                    return json.loads(user_elem.get_attribute('value') or {}).get('token')

        logger.info('Logging in...')
        for _ in range(10):
            self._js_token = get_js_token(driver)

            if self._js_token:
                break

            # try new authentication app option (soft token) first
            try:
                driver.find_element_by_id('iux-mfa-soft-token-verification-code')
                logger.info('Waiting for two factor code...')
                two_factor_code = get_two_factor_code_func()
                logger.info('Sending two factor code: {}'.format(two_factor_code))
                wait_and_click_by_id('iux-mfa-soft-token-verification-code').send_keys(two_factor_code)
                wait_and_click_by_id('VerifySoftTokenSubmitButton', by_testid=True)
                time.sleep(2)
            except (NoSuchElementException, ElementNotVisibleException, StaleElementReferenceException, ElementNotInteractableException):
                pass

            # then try old version of the 2fa soft token page
            try:
                driver.find_element_by_id('ius-mfa-soft-token')
                logger.info('Waiting for two factor code...')
                two_factor_code = get_two_factor_code_func()
                logger.info('Sending two factor code: {}'.format(two_factor_code))
                wait_and_click_by_id('ius-mfa-soft-token').send_keys(two_factor_code)
                wait_and_click_by_id('ius-mfa-soft-token-submit-btn')
                time.sleep(2)
            except (NoSuchElementException, ElementNotVisibleException, StaleElementReferenceException, ElementNotInteractableException):
                pass

            # then try regular 2 factor
            try:
                driver.find_element_by_id('ius-mfa-options-submit-btn')
                self._two_factor_login(get_two_factor_code_func, driver)
            except (NoSuchElementException, ElementNotVisibleException, StaleElementReferenceException):
                pass

            # skip any user verification screen
            try:
                element = driver.find_element_by_id('ius-verified-user-update-btn-skip')
                if element.is_displayed and element.is_enabled:
                    element.click()
                    logger.info('Skipping phone verification step')
            except (NoSuchElementException, ElementNotVisibleException):
                pass

            time.sleep(2)
            logger.debug('Current page title: ' + driver.title)

        if not self._js_token:
            driver.get_screenshot_as_file('/tmp/mint/login_failed.png')
            raise RuntimeError('Failed to get js token from overview page; screenshot output to /tmp/mint/login_failed.png')

        for cookie_json in driver.get_cookies():
            self.session.cookies.set(**{k: v for k, v in cookie_json.items()
                                        if k not in ['httpOnly', 'expiry', 'expires', 'domain', 'sameSite']})

        if not debug:
            driver.close()
            time.sleep(2)
            driver.quit()

        self.get_categories.cache_clear()
        self.get_tags.cache_clear()
        return self

    def get_account_value_history(
            self, acct_ids: List[int],
            start_date: Union[date, datetime],
            end_date: Union[date, datetime]
    ) -> Mapping[str, float]:
        resp = self._get_json_response('trendData.xevent', params={
        "token": self._js_token,
        "searchQuery": json.dumps({
            "reportType": "AT",
            "chartType": "H",
            "comparison": "",
            "matchAny": True,
            "terms": [],
            "accounts": {"groupIds": [], "accountIds": acct_ids, "count": len(acct_ids)},
            "dateRange": {
                "period": {"label": "All time","value": "AT"},
                "start": start_date.strftime("%m/%d/%Y"),
                "end": end_date.strftime("%m/%d/%Y")},
            "drilldown": None,"categoryTypeFilter": "all"})
        })

        return {l['endString']: l['value'] for l in resp['trendList']}

    def cached_login(self, email, password, get_two_factor_code_func=None, debug=False, custom_cahce_location=None) -> 'Mint':
        '''
        See information for login().

        This caches successful login to filesystem, so multiple process can re-use the same login.
        '''
        CACHE_VERSION = 0

        cache_dir = Path(custom_cahce_location) if custom_cahce_location is not None else (Path.home() / '.cache/yamintapi')
        cache_dir.mkdir(exist_ok=True, parents=True)
        cache_file = cache_dir / 'cached_login.pkl'

        if cache_file.exists():
            with open(cache_file, 'rb') as f:
                cached = pickle.load(f)
            if cached['version'] == CACHE_VERSION and cached['email'] == email:
                self._js_token = cached['js_token']
                self.session.cookies = cached['cookies']

                try:
                    if self.is_logged_in(check=True):
                        logger.info('Using cached login')
                        return self
                except MintSessionExpiredException:
                    pass

        self.login(email, password, get_two_factor_code_func=get_two_factor_code_func, debug=debug)

        with open(cache_file, 'wb') as f:
            logger.info('Caching login to file {}'.format(cache_file))
            pickle.dump({
                'version': CACHE_VERSION,
                'js_token': self._js_token,
                'cookies': self.session.cookies,
                'email': email,
            }, f)

        return self

    def is_logged_in(self, check=False) -> bool:
        if not check:
            return self._js_token is not None

        resp = self._get_json_response('userStatus.xevent', params={'rnd': random.randint(0, 10**14)}, method='get')
        return 'isRefreshing' in resp

    def change_transaction_page_limit(self, page_size=100):
        """
        Change how default number of transactions returned per page (it seems only 25, 50, 100 work)
        """
        params = {
            "task": "transactionResults",
            "data": page_size,
            "token": self._js_token,
        }
        return self._get_json_response('updatePreference.xevent', data=params, expect_json=False)

    def _get_json_response(self, url, params: dict = None, data: dict = None, method='post', expect_json=True, unescape_html=False) -> dict:
        response = self.session.request(method=method,
                                        url=os.path.join(_MINT_ROOT_URL, url),
                                        params=params,
                                        data=data,
                                        headers={'accept': 'application/json', 'token': self._js_token})

        self._last_request_result = response.text

        is_json_resp = re.match('text/json|application/json', response.headers.get('content-type', ''))

        if (response.status_code != requests.codes.ok or (expect_json and not is_json_resp)):
            if 'session has expired' in response.text.lower():
                raise MintSessionExpiredException()
            else:
                logger.error('_get_json_response failed response: {}'.format(self._last_request_result))
                raise RuntimeError('Request for {} {} {} failed: {} {}'.format(url, params, data, response.status_code, response.headers))

        resp_text = response.text
        if unescape_html:
            resp_text = html.unescape(resp_text)

        return json.loads(resp_text)

    def _get_service_response(self, data: dict) -> dict:
        data = data.copy()
        data['id'] = str(random.randint(0, 10**14))

        result = self._get_json_response('bundledServiceController.xevent',
                                         params={'legacy': False, 'token': self._js_token},
                                         data={'input': json.dumps([data])})

        if data['id'] not in result.get('response', []):
            raise RuntimeError('bundleServiceController request for {} failed, response: {}'.format(data, result))

        return result['response'][data['id']]['response']

    def _get_financial_provider_response(self, url, method='get', data=None):
        # for some reason, this call sometimes messes up the cookies
        prev_cookies = self.session.cookies

        full_url = os.path.join(_MINT_ROOT_URL, 'mas', url.strip('/')) if url.startswith('/') else url

        headers = {
            'Authorization': 'Intuit_APIKey intuit_apikey=prdakyrespQBtEtvaclVBEgFGm7NQflbRaCHRhAy, intuit_apikey_version=1.0',
            "content-type": "application/json",
            "intuit_appid": "1040",
            "intuit_country": "US",
            "intuit_iddomain": "GLOBAL",
            "intuit_locale": "en_US",
            "intuit_offeringid": "mint.intuit.com",
            "intuit_originatingip": "127.0.0.1",
            "intuit_tid": "mw-090-88947e05-e57a-4f84-b5f9-debdfbd43653",
        }

        logger.debug('_get_financial_provider_response[{}]'.format(full_url))

        res = self.session.request(method=method, url=full_url, headers=headers, data=json.dumps(data))

        self._init_session(prev_cookies)

        return res

    def _get_jsondata_response_generator(self, params, initial_offset=0):
        params = params.copy()
        offset = initial_offset
        while True:
            params['offset'] = offset
            params['rnd'] = random.randint(0, 10**14)
            resp = self._get_json_response('app/getJsonData.xevent', params=params, method='get')
            results = [r for r in resp['set'] if r['id'] == 'transactions'][0].get('data', [])
            offset += len(results)
            for result in results:
                yield result
            if not results:
                break

    def _two_factor_login(sel, get_two_factor_code_func, driver: 'selenium.webdriver'):
        if not get_two_factor_code_func:
            raise Exception('2 factor login is required but `get_two_factor_code_func` is not provided.\n'
                            'Try e.g. mint.login(..., get_two_factor_code_func=lambda: getpass.getpass("Enter 2 factor code sent to your email: "))')

        driver.implicitly_wait(3)
        driver.find_element_by_id('ius-mfa-option-email').click()
        driver.find_element_by_id('ius-mfa-options-submit-btn').click()

        logger.info('Waiting for two factor code...')
        two_factor_code = get_two_factor_code_func()

        logger.info('Sending two factor code: {}'.format(two_factor_code))
        driver.find_element_by_id('ius-mfa-confirm-code').send_keys(two_factor_code)

        driver.find_element_by_id('ius-mfa-otp-submit-btn').click()
        driver.implicitly_wait(0)


class MintSessionExpiredException(Exception):
    pass
