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
from typing import Sequence as Seq, Mapping, Union, List, Literal
import logging


logger  = logging.getLogger(__name__)


_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36'
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

    @lru_cache
    def get_user(self) -> dict:
        return self._get_pfm_response('/v1/user')

    @lru_cache
    def get_account_id(self) -> str:
        return self.get_user()['id']

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
        return self._get_pfm_response('/v1/accounts?offset=0&limit=1000')['Account']

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

    def _get_account_by_name(self, name: str, provider_name: str = None, error_if_duplicates=True) -> dict:
        providers = self.get_financial_providers().get('providers', [])
        if provider_name is not None:
            providers = [p for p in providers if p['name'] == provider_name]

        matching_accts = [account for provider in providers for account in provider['providerAccounts']
                           if account['name'] == name]

        if len(matching_accts) == 0:
            raise RuntimeError('Account by name {} under with provider name {} is not found'.format(name, provider_name))

        if len(matching_accts) > 1 and error_if_duplicates:
            raise RuntimeError('There are multiple account by name {} under with provider name {}'.format(name, provider_name))

        return matching_accts[0]

    def update_manual_asset_value(self, name: str, value: float):
        """
        Update value for manually entered assets.
        """
        acct = self._get_account_by_name(name, 'Other Property')
        url = acct['metaData']['link'][0]['href']

        params = {
            "name": acct['name'],
            "type": "OtherPropertyAccount",
            "associatedLoanAccounts": acct['associatedLoanAccounts'],
            "hasAssociatedLoanAccounts": len(acct['associatedLoanAccounts']) > 0,
            "value": value
        }

        res = self._get_financial_provider_response(url, method='patch', data=params)
        res.raise_for_status()

    def get_transactions(self,
                         account_id: str = None,
                         account_type: Literal['CashAccount', 'InvestmentAccount', 'CreditAccount', 'BankAccount'] = None,
                         start_date = '2007-01-01',
                         end_date = '2030-01-01',
                         page_size = 100,
                         sort_by: Literal['date', 'merchant', 'amount', 'category'] = 'date',
                         sort_asc = False):
        if account_id is not None:
            search_filters = [{"matchAll": True, "filters": [{"type": "AccountIdFilter", "accountId": account_id}]}]
        elif account_type is not None:
            search_filters = [{"matchAll": True, "filters": [{"type": "AccountTypeFilter", "accountType": account_type}]}]
        else:
            search_filters = []

        search_data = {
            "limit": page_size,
            "searchFilters": search_filters,
            "dateFilter": {"type":"CUSTOM", "endDate": end_date, "startDate": start_date},
            "sort": sort_by.upper() +  ('' if sort_asc else '_DESCENDING'),
        }

        transactions = []
        while True:
            result = self._post_pfm_response('/v1/transactions/search', {**search_data, "offset": len(transactions)})

            transactions += result['Transaction']

            total_size = result.get('metaData',{}).get('totalSize', 0)
            if len(transactions) >= total_size:
                break

        return transactions

    def get_transaction_by_id(self, transaction_id: Union[str, int]):
        """
        transaction_id can either the full form <account_id>_123456_1, or a number, e.g. 123456. If it is a number
        the transaction is assume to be a non-investment transaction.
        """
        if isinstance(transaction_id, int) or '_' not in transaction_id:
            transaction_id = '{}_{}_0'.format(self.get_account_id(), transaction_id)

        return self._get_pfm_response('/v1/transactions/{}'.format(transaction_id))

    def update_transaction(self,
                           transaction_id: Union[str, List[str]],
                           description: str = None,
                           category_name: str = None,
                           category_id: int = None,
                           is_duplicate: bool = None,
                           note: str = None,
                           transaction_date: date = None,
                           tags: Mapping[str, bool] = {},
                           amount: float = None,
    ) -> bool:
        '''
        transaction_id can be obtained from get_transactions().

        It can be a single id, or a list of ids.

        To add/remove tag, pass `tags={'tag_name': True/False}`. Tags not present in `tags` will remain unchanged.

        Only one of category_name and category_id is needed (category_id takes priority). Usually category_name
        suffices, unless there are multiple categories with the same name (but under different parent categories).

        The amount arg is only valid for cash transactions. The request will fail if trying to modify amount for
        a non-cash transaction.
        '''

        category_id, category_name = self._validate_category(category_id, category_name)

        trans_ids = transaction_id if isinstance(transaction_id, list) else [transaction_id]

        tags_by_tran_ids = {}
        if tags != {}:
            trans = {tid: self.get_transaction_by_id(tid) for tid in trans_ids}
            for tid, tr in trans.items():
                # looks like {..., 'tagData': {tags: [{id: "123456_567890"}]} }
                tag_ids = {tag['id'] for tag in tr.get('tagData', {'tags': []})['tags']}
                tag_updates = {self.tag_name_to_id(tag): checked for tag, checked in tags.items()}
                tag_ids |= {tag_id for tag_id, checked in tag_updates.items() if checked}
                tag_ids -= {tag_id for tag_id, checked in tag_updates.items() if not checked}

                tags_by_tran_ids[tid] = tag_ids

        data = [{
            'id': tid,
            'type': 'CashAndCreditTransaction' if tid.endswith('_0') else 'InvestmentTransaction',
            **{k: v for k, v in {
                'description': description,
                'tagData': {"tags": [{"id": tag_id} for tag_id in tags_by_tran_ids[tid]]} if tags != {} else None,
                'category': {"id": category_id} if category_id is not None else None,
                'isDuplicate': is_duplicate if is_duplicate is not None else None,
                'notes': note,
                'date': transaction_date.strftime('%Y-%m-%d') if transaction_date else None,
                'amount': str(amount) if amount is not None else None
            }.items() if v is not None}
        } for tid in trans_ids]

        logger.info('update_transaction {}'.format(data))

        self._put_pfm_response('/v1/transactions/', data={"Transaction": data})

        return True

    def split_transaction(self, transaction_id: Union[str, int], split_transactions: List[dict]) -> dict:
        """
        Split transactions. Return the split transaction, with a `childrend` attribute containing the splits.

        To unsplit the transaction, simply provide an empty list to split_transactions.
        If the sum of split_transactions doesn't match the origianl amount, Mint will automatically create
        one more split transaction with the remainder.

        >>> mint.split_transaction(3985739713, [
        >>>      {'amount': 10, 'description': 'description 1', 'category_name': 'Transfer'},
        >>>      {'amount': 26.34, 'description': 'description 2', 'category_id': '12345_12345'}
        >>> ])

        Return the split transactions
        """

        trans = self.get_transaction_by_id(transaction_id)
        transaction_id = trans['id']

        if transaction_id.endswith('_1'):
            raise RuntimeError('Not valid to split investment transactions?')

        input_split_trans = []
        for tr in split_transactions:
            category_id, category_name = self._validate_category(tr.get('category_id'), tr.get('category_name'))
            input_split_trans.append({
                'amount': tr['amount'],
                'description': tr['description'],
                "category": {'id': category_id}
            })

        data = {
            'type': 'CashAndCreditTransaction',
            'amount': trans['amount'],
            'splitData': { 'children': input_split_trans}
        }

        self._put_pfm_response('/v1/transactions/{}'.format(transaction_id), data, json_response=False)
        return self.get_transaction_by_id(transaction_id)

    def unsplit_transaction(self, transaction_id: Union[str, int]) -> dict:
        """
        Unsplit a split transaction. `transaction_id` can be either the parent transaction or one of the children.
        """
        trans = self.get_transaction_by_id(transaction_id)

        if 'splitData' in trans:
            return self.split_transaction(trans['id'], [])
        elif 'parentId' in trans:
            return self.split_transaction(trans['parentId'], [])
        else:
            raise RuntimeError('{} does not look it is a split or child of a split transaction'.format(transaction_id))

    def delete_transaction(self, transaction_id: str):
        trans = self.get_transaction_by_id(transaction_id)

        if not (trans['isPending'] or trans.get('manualTransactionType') == 'CASH'):
            raise RuntimeError('transacation_id {} is not a pending or cash transaction. Probably not a good idea to delete'.format(transaction_id))

        self._delete_pfm_response('/v1/transactions/{}'.format(trans['id']), json_response=False)

    def add_cash_transaction(self,
                             description: str,
                             amount: float,
                             category_name: str = None, category_id: int = None,
                             note: str = None,
                             transaction_date: date = None,
                             tags: Seq[str] = [],
                             is_expense: bool = None,
                             should_pull_from_atm_withdrawal: bool = False) -> dict:
        '''
        If amount if positive, transaction will be created as an income. Else, it is created as an expense.

        Only one of category_name and category_id is needed (category_id takes priority). Usually category_name
        suffices, unless there are multiple categories with the same name (but under different parent categories).
        '''
        category_id, category_name = self._validate_category(category_id, category_name)

        self._post_pfm_response('/v1/transactions', {
            "type": "CashAndCreditTransaction",
            "manualTransactionType": "CASH",
            "date": (transaction_date or date.today()).strftime('%Y-%m-%d'),
            "description": description,
            "category": {"id": category_id},
            "amount": amount,
            "isExpense": is_expense if is_expense is not None else amount < 0,
            "tagData": {'tags': [{'id': self.tag_name_to_id(t)} for t in tags]} if len(tags) > 0 else None,
            "shouldPullFromAtmWithdrawals": should_pull_from_atm_withdrawal,
        }, json_response=False)

    @lru_cache()
    def get_categories(self) -> Seq[dict]:
        return self._get_pfm_response('/v1/categories')['Category']

    def _get_category_by_id(self, category_id: Union[str, int]) -> dict:
        if isinstance(category_id, int) or '_' not in category_id:
            category_id = '{}_{}'.format(self.get_account_id(), category_id)

        categories = self.get_categories()
        try:
            return [c for c in categories if c['id'] == category_id][0]
        except:
            raise RuntimeError('category_id {} seems to not exist'.format(category_id))

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
    def _is_category_user_created(self, category_id: Union[str, int]) -> bool:
        category_id = int(category_id) if isinstance(category_id, int) or '_' not in category_id else int(category_id.split('_')[1])
        return category_id > 10000;

    def create_category(self, name: str, parent_category_id: Union[int, str]) -> int:
        """ Returns new cateogry id if successful """
        parent_category = self._get_category_by_id(parent_category_id)

        if parent_category['depth'] != 1:
            raise RuntimeError(f'Cannot only create cateogry under sub category: {parent_category}')

        data = {
            'depth': parent_category['depth'] + 1,
            'name': name,
            'parentId': parent_category['id'],
        }

        self._post_pfm_response('/v1/categories', data, json_response=False)

        self.get_categories.cache_clear()
        return self.category_name_to_id(name)

    def rename_category(self, category_id: Union[int, str], name: str) -> bool:
        category = self._get_category_by_id(category_id)

        if not self._is_category_user_created(category_id):
            raise RuntimeError('Cannot only change user category')

        self._put_pfm_response('/v1/categories/{}'.format(category['id']), {
            'depth': category['depth'],
            'name': name,
            'parentId': category['parentId'],
        }, json_response=False)

        self.get_categories.cache_clear()

    def delete_category(self, category_id: Union[str, int]):
        category = self._get_category_by_id(category_id)

        if not self._is_category_user_created(category_id):
            raise RuntimeError('Cannot only change user category')

        self._delete_pfm_response('/v1/categories/{}'.format(category['id']), json_response=False)
        self.get_categories.cache_clear()

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

        data = self._get_pfm_response('/v1/tags')['Tag']
        return {t['name']: t for t in data}

    def tag_name_to_id(self, name) -> int:
        tag_id = self.get_tags().get(name, {}).get('id', None)
        if not tag_id:
            raise RuntimeError('Tag {} does not exist. Create it first with create_tag()'.format(name))
        return tag_id

    def create_tag(self, name) -> int:
        ''' Return the id of newly created tag'''

        if name in self.get_tags():
            raise Exception('{} is already a tag'.format(name))

        self._post_pfm_response('/v1/tags', {"name": name}, json_response=False)

        self.get_tags.cache_clear()
        return self.get_tags().get(name, {}).get('id', None)

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

        overview_url = os.path.join(_MINT_ROOT_URL, 'overview')
        driver.get(overview_url)

        def wait_and_click_by_id(elem_id, timeout=10, check_freq=1, by_testid=False):
            ''' more debug message and finer control over selenium's wait functionality '''
            for _ in range(timeout // check_freq):

                # hacky escape hatch
                if driver.current_url.startswith(overview_url):
                    return

                try:
                    if not by_testid:
                        element = driver.find_element_by_id(elem_id)
                    else:
                        element = driver.find_element_by_xpath(f'//*[@data-testid = "{elem_id}"]')

                    if element.is_displayed and element.is_enabled:
                        element.click()
                        return element
                except (NoSuchElementException, ElementNotVisibleException, StaleElementReferenceException, ElementNotInteractableException):
                    pass
                time.sleep(check_freq)
                if debug:
                    logger.info('Waiting for id={} to be clickable'.format(elem_id))

            driver.get_screenshot_as_file('/tmp/mint_error.png')
            raise Exception('Fail to find id={} to click on'.format(elem_id))

        logger.info('Waiting for login page to load...')

        try:
            wait_and_click_by_id('iux-identifier-first-unknown-identifier').send_keys(email)
            wait_and_click_by_id('IdentifierFirstSubmitButton', by_testid=True)
            wait_and_click_by_id('iux-password-confirmation-password').send_keys(password)
            wait_and_click_by_id('passwordVerificationContinueButton', by_testid=True)
        except Exception:
            driver.get_screenshot_as_file('/tmp/mint_error.png')
            raise

        logger.info('Logging in...')
        for _ in range(10):
            if driver.current_url.startswith(overview_url):
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
                    wait_and_click_by_id('ius-verified-user-update-btn-skip')
                    logger.info('Skipping phone verification step')
            except (NoSuchElementException, ElementNotVisibleException):
                pass

            # skip prompt to login in other ways
            try:
                element = driver.find_element_by_id('skipWebauthnRegistration')
                if element.is_displayed and element.is_enabled:
                    wait_and_click_by_id('skipWebauthnRegistration')
                    logger.info('Skipping other auth option step')
            except (NoSuchElementException, ElementNotVisibleException):
                pass

            time.sleep(2)
            logger.debug('Current page title: ' + driver.title)

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
                'cookies': self.session.cookies,
                'email': email,
            }, f)

        return self

    def is_logged_in(self, check=False) -> bool:
        if check:
            self.get_categories.cache_clear()

        try:
            self.get_categories()
            return True
        except:
            return False

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
            "authorization": "Intuit_APIKey intuit_apikey=prdakyrespQBtEtvaclVBEgFGm7NQflbRaCHRhAy, intuit_apikey_version=1.0",
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

        res = self.session.request(method=method, url=full_url, headers=headers, data=json.dumps(data) if data else None)

        self._init_session(prev_cookies)

        return res

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

    def _pfm_request(self, method, url, json_response=True, **kwargs):
        headers = {
            "authorization": "Intuit_APIKey intuit_apikey=prdakyresYC6zv9z3rARKl4hMGycOWmIb4n8w52r,intuit_apikey_version=1.0",
            "content-type": "application/json",
            "intuit_tid": "mw-190-1377ef39-640c-42ac-87e6-e2e68bd3bf32",
            "pragma": "no-cache",
        }

        ROOT_URL = 'https://mint.intuit.com/pfm'

        resp = self.session.request(method, ROOT_URL + url, headers=headers, **kwargs)

        try:
            resp.raise_for_status()

            return resp.json() if json_response else resp
        except:
            logger.info('{} pfm response {} failed: {}'.format(method, url, resp.text))
            raise

    def _get_pfm_response(self, url, **kwargs):
        return self._pfm_request('GET', url, **kwargs)

    def _post_pfm_response(self, url, data, **kwargs):
        return self._pfm_request('POST', url, json=data, **kwargs)

    def _put_pfm_response(self, url, data, **kwargs):
        return self._pfm_request('PUT', url, json=data, **kwargs)

    def _delete_pfm_response(self, url, **kwargs):
        return self._pfm_request('DELETE', url, **kwargs)

class MintSessionExpiredException(Exception):
    pass
