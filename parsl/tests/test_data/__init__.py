
def setup_package():
    import os
    os.mkdir("data")
    with open("data/test1.txt", 'w') as f:
        f.write("1\n")
    with open("data/test2.txt", 'w') as f:
        f.write("2\n")

def teardown_package():
    pass
