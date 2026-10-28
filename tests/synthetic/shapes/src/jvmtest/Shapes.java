package jvmtest;

// Statement shapes the Pseudo Java printer must rebuild (tests/bn_shapes_check.py lists what each prints).
public class Shapes {
    int count;

    static void work(int n) {
        System.out.println(n);
    }

    // jvm-112: a try/finally in each branch of the method's last if; javac copies the finally before the
    // implicit return at each branch's end
    static void tailIfFinally(boolean c, int n) {
        if (c) {
            try {
                work(n);
            } finally {
                work(1);
            }
        } else {
            try {
                work(n + 1);
            } finally {
                work(2);
            }
        }
    }

    // jvm-112: the same one level deeper (a trailing if inside the trailing if)
    static void nestedTailIfFinally(boolean c, boolean d, int n) {
        work(0);
        if (c) {
            if (d) {
                try {
                    work(n);
                } finally {
                    work(3);
                }
            }
        }
    }

    // jvm-111: no else after a branch that ends in break
    int breakNoElse(int i, int[] a) {
        int s = 0;
        while (true) {
            if (i <= 0) {
                count++;
                break;
            }
            i--;
            s += a[i];
            work(s);
        }
        return s;
    }

    // jvm-111: no else after a branch that ends in return
    static int returnNoElse(String s) {
        if (s == null) {
            work(-1);
            return 0;
        }
        int n = s.length();
        work(n);
        return n * 2;
    }

    // jvm-111: no else after a branch that ends in throw
    static int throwNoElse(int n) {
        if (n < 0) {
            work(n);
            throw new IllegalArgumentException("negative");
        }
        work(n);
        return n + 1;
    }

    // jvm-111: Vineflower's TestSynchronizedLoop.test12 -- HLIL gives `if (i <= 0) { ..; break; } else { .. }`
    public void syncLoopElse(int i) {
        label:
        synchronized (this) {
            System.out.println(1);
            while (i > 0) {
                i--;
                System.out.println(1.5);
                try {
                    System.out.println(1.6);
                } finally {
                    System.out.println(1.7);
                    if (i > 5) {
                        break label;
                    }
                }
            }
            System.out.println(2);
            if (i > -2) {
                System.out.println(3);
            }
        }
        if (i > 2) {
            System.out.println("Hello!");
        }
    }

    // jvm-111: a loop left by a return, the rest of its body in a try/finally
    static int loopReturnElse(int i) {
        while (true) {
            if (i <= 0) {
                work(0);
                return i;
            }
            i--;
            try {
                work(i);
            } finally {
                work(-i);
            }
        }
    }

    // jvm-93: javac's null check of a bound method reference (o.getClass() in Java 8) inside a try: the call's
    // exception test sits between it and the statement creating o::toString
    static java.util.function.Supplier<String> boundRefInTry(Object o) {
        try {
            return o::toString;
        } catch (RuntimeException e) {
            return null;
        }
    }

    // jvm-93: a lambda in an if condition capturing a computed value (BN propagates it into the invokedynamic)
    static void lambdaInCondition(java.util.List<Integer> list) {
        int bottom = list.size() * 2;
        if (list.stream().anyMatch(x -> x > bottom)) {
            work(1);
        }
        work(2);
    }
}
