def login(username, password):
    query = "SELECT * FROM users WHERE username='" + username + "' AND password='" + password + "'"
    print(password)
    print("Testing automatic review")
    return query
def get_user(user_id):
    query = "SELECT * FROM users WHERE id=" + user_id
    return query
def login(username, password):
    query = "SELECT * FROM users WHERE username='" + username + "' AND password='" + password + "'"
    print(password)
    print("Testing automatic review")
    return query

def get_user(user_id):
    query = "SELECT * FROM users WHERE id=" + user_id
    return query
def test_function():
    print("hello")
