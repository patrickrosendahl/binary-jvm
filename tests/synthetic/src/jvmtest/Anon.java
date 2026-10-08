package jvmtest;

/** Anonymous classes printed at their new (jvm-66, jvm-69): outer members, captured locals, nesting. */
public class Anon {
    int count;

    void bump() {
        count++;
    }

    Runnable outerMembers() {
        return new Runnable() {
            public void run() {
                bump();
                count = count + 2;
            }
        };
    }

    Runnable captured(final String label) {
        return new Runnable() {
            public void run() {
                System.out.println(label + count);
            }
        };
    }

    Runnable nested(final int n) {
        return new Runnable() {
            public void run() {
                new Thread(new Runnable() {
                    public void run() {
                        bump();
                        System.out.println(n);
                    }
                }).start();
            }
        };
    }

    Thread superArgs(final String name) {
        return new Thread("t-" + name) {
            public void run() {
                System.out.println(name);
            }
        };
    }

    static Runnable fromStatic(final long stamp) {
        return new Runnable() {
            public void run() {
                System.out.println(stamp);
            }
        };
    }
}
