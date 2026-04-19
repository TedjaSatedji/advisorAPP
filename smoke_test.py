import requests
import uuid

BASE_URL = 'http://127.0.0.1:5000'

def test():
    s1 = requests.Session()
    s2 = requests.Session()

    u1 = f'user_{uuid.uuid4().hex[:8]}'
    p1 = 'password123'
    u2 = f'user_{uuid.uuid4().hex[:8]}'
    p2 = 'password123'

    # Register u1
    r1 = s1.post(f'{BASE_URL}/register', data={'username': u1, 'password': p1, 'confirm': p1})
    if r1.status_code != 200:
        print(f'Error registering u1: {r1.status_code}')
    
    # Register u2
    r2 = s2.post(f'{BASE_URL}/register', data={'username': u2, 'password': p2, 'confirm': p2})
    if r2.status_code != 200:
        print(f'Error registering u2: {r2.status_code}')

    # Create unique categories for u1
    exp_cat = f'Exp_{uuid.uuid4().hex[:6]}'
    inc_cat = f'Inc_{uuid.uuid4().hex[:6]}'
    
    s1.post(f'{BASE_URL}/categories/add', data={'name': exp_cat})
    s1.post(f'{BASE_URL}/income-categories/add', data={'name': inc_cat})

    # Check u2 categories
    resp = s2.get(f'{BASE_URL}/categories')
    content = resp.text
    
    found_exp = exp_cat in content
    found_inc = inc_cat in content
    
    print(f'User A categories: {exp_cat}, {inc_cat}')
    print(f'User B sees User A expense category: {found_exp}')
    print(f'User B sees User A income category: {found_inc}')

if __name__ == "__main__":
    try:
        test()
    except Exception as e:
        print(f'Error: {e}')
