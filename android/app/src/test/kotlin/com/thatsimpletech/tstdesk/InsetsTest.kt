package com.thatsimpletech.tstdesk

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The composer lift on the phone. The keyboard is a window inset on target
 * 35, and the bottom padding follows whichever of the two bottom insets is
 * deepest: the keyboard's top while it is up, the navigation bar otherwise.
 */
class InsetsTest {

    @Test
    fun theKeyboardLiftsTheBottomPaddingWhenItIsUp() {
        // A 260px keyboard over a 48px navigation bar: the keyboard wins.
        assertEquals(260, bottomInset(48, 260))
    }

    @Test
    fun theNavigationBarStaysWhenTheKeyboardIsDown() {
        // The IME reports 0 while it is down, so the bar keeps its room.
        assertEquals(48, bottomInset(48, 0))
    }

    @Test
    fun theDeeperInsetWinsWhicheverIsNamedFirst() {
        assertEquals(48, bottomInset(48, 12))
        assertEquals(48, bottomInset(12, 48))
    }
}
