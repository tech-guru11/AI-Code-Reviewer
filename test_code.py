def login(username, password):
    query = "SELECT * FROM users WHERE username='" + username + "' AND password='" + password + "'"
    print(password)
    print("Testing automatic review")
    return query
